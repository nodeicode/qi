// vq_kernels.cpp — nanobind native kernels for common quant/utils hot paths.
//
// Common (strategy-agnostic) scalar/recurrence loops from quant/utils/indicators.py
// that cannot be vectorized in pure Python. Pure sequential recurrences; the C++
// port removes per-element Python overhead. A Python fallback is used when this
// module is unavailable.
//
// Parity (SLO parity >= 99%): each kernel reproduces the Python recurrence
// bit-for-bit for the same inputs/outputs.
#include <nanobind/nanobind.h>
#include <nanobind/ndarray.h>
#include <nanobind/stl/tuple.h>
#include <complex>
#include <cstdint>
#include <cmath>
#include <mutex>
#include <unordered_map>
#include <vector>

namespace nb = nanobind;

using nd1 = nb::ndarray<double, nb::numpy, nb::ndim<1>>;

static inline nd1 make_out(std::vector<double>* v) {
    nb::capsule owner(v, [](void* p) noexcept { delete static_cast<std::vector<double>*>(p); });
    nd1 out(v->data(), {(size_t)v->size()}, owner);
    return out;
}

// adaptive_z: mirrors _adaptive_z_score sequential recurrence.
static nd1 adaptive_z_scores(nd1 a_in, nd1 sc_in) {
    auto n = a_in.shape(0);
    auto* out = new std::vector<double>(n);
    const double* a = a_in.data();
    const double* sc = sc_in.data();
    double* z = out->data();
    if (n > 0) {
        double means = a[0];
        double vars_ = 0.0;
        z[0] = 0.0;
        for (size_t i = 1; i < n; ++i) {
            double sci = sc[i];
            means += sci * (a[i] - means);
            double dm = a[i] - means;
            vars_ = (1.0 - sci) * vars_ + sci * dm * dm;
            z[i] = (vars_ > 1e-9) ? (dm / std::sqrt(vars_)) : 0.0;
        }
    }
    return make_out(out);
}

// kalman: mirrors fast_kalman_filter (random-walk 1D).
nd1 kalman_filter(nd1 prices_in, double Q, double R) {
    size_t n = prices_in.shape(0);
    auto* out = new std::vector<double>(n);
    const double* pr = prices_in.data();
    double* est = out->data();
    if (n > 0) {
        double estimate = pr[0];
        double error_est = 1.0;
        for (size_t i = 0; i < n; ++i) {
            double estimate_pred = estimate;
            double error_est_pred = error_est + Q;
            double k_gain = error_est_pred / (error_est_pred + R);
            estimate = estimate_pred + k_gain * (pr[i] - estimate_pred);
            error_est = (1.0 - k_gain) * error_est_pred;
            est[i] = estimate;
        }
    }
    return make_out(out);
}

// fisher_core: mirrors the _fisher_transform recurrence (period..n).
nd1 fisher_core(nd1 prices, nd1 roll_min, nd1 roll_max, int64_t period) {
    size_t n = prices.shape(0);
    auto* out = new std::vector<double>(n, 0.0);
    const double* p = prices.data();
    const double* mn = roll_min.data();
    const double* mx = roll_max.data();
    double* f = out->data();
    double val = 0.0;
    double prev_f = 0.0;
    for (size_t i = (size_t)period; i < n; ++i) {
        double curr_max = mx[i];
        double curr_min = mn[i];
        if (curr_max != curr_min)
            val = 0.33 * 2.0 * ((p[i] - curr_min) / (curr_max - curr_min) - 0.5) +
                  0.67 * val;
        else
            val = 0.0;
        val = val < -0.99 ? -0.99 : (val > 0.99 ? 0.99 : val);
        double fisher_ = 0.5 * std::log((1.0 + val) / (1.0 - val)) + 0.5 * prev_f;
        f[i] = fisher_;
        prev_f = fisher_;
    }
    return make_out(out);
}

// hilbert_spectrum — causal Hilbert via last-sample analytic projection.
//
// Mirrors indicators.calculate_hilbert_transform: detrend (diff, prepend
// values[0]), then per sliding window compute the analytic signal's LAST sample
// via scipy.signal.hilbert semantics (fft -> zero negative freqs -> ifft), take
// |z| and atan2(z.imag, z.real), unwrap the phase exactly like np.unwrap, and
// derive frequency as diff(unwrapped) / 2pi.
//
// Instead of an FFT per window, note the last sample of the analytic signal is
// a LINEAR function of the window: analytic_last(N, x) = sum_j w_j(N) * x_j
// with w_j(N) = (1/N) sum_k h_N[k] * exp(2*pi*i*k*(N-1-j)/N), h_N the scipy
// hilbert frequency mask. Weights are precomputed once per length N (cached);
// each window is then an O(N) dot product (vs O(N log N) FFT), with accuracy
// ~1e-13 relative to scipy's pocketfft (parity contract: allclose 1e-9/1e-12,
// same as the other kernels).

namespace {
using cd = std::complex<double>;

// Portable pi (M_PI is a POSIX-only macro, not defined on libstdc++ with
// -std=c++17). cosl/sinl are C-library functions in the GLOBAL namespace — do
// not qualify as std::cosl (GCC/libstdc++ does not provide the std:: overloads).
constexpr double kPiD = 3.14159265358979323846;

std::vector<cd> hilbert_weights(size_t N) {
    static std::unordered_map<size_t, std::vector<cd>> cache;
    static std::mutex mtx;
    {
        std::lock_guard<std::mutex> lock(mtx);
        auto it = cache.find(N);
        if (it != cache.end()) return it->second;
    }
    // scipy.signal.hilbert frequency mask h_N[k].
    std::vector<int> h(N, 0);
    h[0] = 1;
    if (N % 2 == 0) {
        if (N >= 2) h[N / 2] = 1;
        for (size_t k = 1; k < N / 2; ++k) h[k] = 2;
    } else {
        for (size_t k = 1; k < (N + 1) / 2; ++k) h[k] = 2;
    }
    std::vector<cd> w(N);
    const long double two_pi = 6.283185307179586476925286766559L;
    for (size_t j = 0; j < N; ++j) {
        long double re = 0.0L, im = 0.0L;
        for (size_t k = 0; k < N; ++k) {
            if (h[k] == 0) continue;
            long double ang =
                two_pi * (long double)k * (long double)(N - 1 - j) / (long double)N;
            long double c = ::cosl(ang);
            long double s = ::sinl(ang);
            re += (long double)h[k] * c;
            im += (long double)h[k] * s;
        }
        w[j] = cd((double)(re / (long double)N), (double)(im / (long double)N));
    }
    std::lock_guard<std::mutex> lock(mtx);
    cache.emplace(N, w);
    return w;
}
}  // namespace

// Returns (unwrapped_phase, frequency, amplitude) — mirrors the three
// H_PHASE / H_FREQ / H_AMPLITUDE columns of calculate_hilbert_transform.
std::tuple<nd1, nd1, nd1> hilbert_spectrum(nd1 values, int64_t window) {
    size_t n = values.shape(0);
    auto* ph_v = new std::vector<double>(n, 0.0);
    auto* fr_v = new std::vector<double>(n, 0.0);
    auto* amp_v = new std::vector<double>(n, 0.0);
    const double* v = values.data();
    double* ph = ph_v->data();
    double* fr = fr_v->data();
    double* amp = amp_v->data();
    if (n >= 2 && window >= 1) {
        // detrended = np.diff(values, prepend=values[0]) -> det[0] == 0.0.
        std::vector<double> det(n);
        det[0] = 0.0;
        for (size_t i = 1; i < n; ++i) det[i] = v[i] - v[i - 1];

        size_t w = (size_t)window;
        // Expanding-window warmup: i in 1..min(window, n)-1, len i+1.
        size_t warm_hi = w < n ? w : n;
        for (size_t i = 1; i < warm_hi; ++i) {
            size_t N = i + 1;
            const auto& W = hilbert_weights(N);
            cd acc(0.0, 0.0);
            const double* x = det.data();
            for (size_t j = 0; j < N; ++j) acc += W[j] * x[j];
            amp[i] = std::hypot(acc.real(), acc.imag());
            ph[i] = std::atan2(acc.imag(), acc.real());
        }
        // Batch windows (overwrites warmup at i == w-1 with the same result).
        if (n >= w) {
            const auto& W = hilbert_weights(w);
            for (size_t i = w - 1; i < n; ++i) {
                cd acc(0.0, 0.0);
                const double* x = det.data() + (i - w + 1);
                for (size_t j = 0; j < w; ++j) acc += W[j] * x[j];
                amp[i] = std::hypot(acc.real(), acc.imag());
                ph[i] = std::atan2(acc.imag(), acc.real());
            }
        }
        // np.unwrap(phase_raw, discont=pi): corrections from ORIGINAL diffs,
        // then cumulative add (matches numpy's up[1:] += cumsum(ph_correct)).
        std::vector<double> orig(n);
        for (size_t i = 0; i < n; ++i) orig[i] = ph[i];
        double cum = 0.0;
        for (size_t i = 1; i < n; ++i) {
            double diff = orig[i] - orig[i - 1];
            double ddmod = std::fmod(diff + kPiD, 2.0 * kPiD);
            if (ddmod < 0.0) ddmod += 2.0 * kPiD;  // numpy mod takes divisor sign
            ddmod -= kPiD;
            if (ddmod == -kPiD && diff > 0.0) ddmod = kPiD;
            double ph_corr = (std::fabs(diff) < kPiD) ? 0.0 : (ddmod - diff);
            cum += ph_corr;
            ph[i] = orig[i] + cum;
        }
        // frequency = diff(unwrapped) / (2*pi); frequency[0] stays 0.0.
        for (size_t i = 1; i < n; ++i) fr[i] = (ph[i] - ph[i - 1]) / (2.0 * kPiD);
    }
    return std::make_tuple(make_out(ph_v), make_out(fr_v), make_out(amp_v));
}

// hurst_exponent — mirrors _hurst_exponent: sliding-window batched R/S at
// lags {2,4,8,16} (skip lag >= window/2) with NaN-safe OLS slope fit.
nd1 hurst_exponent(nd1 a_in, int64_t window) {
    size_t n = a_in.shape(0);
    auto* out = new std::vector<double>(n, std::nan(""));
    const double* a = a_in.data();
    double* res = out->data();
    size_t w = (size_t)window;
    if (n < w) return make_out(out);
    const double lag[4] = {2.0, 4.0, 8.0, 16.0};
    const double log_lag[4] = {0.6931471805599453, 1.3862943611198906,
                               2.0794415416798357, 2.7725887222397812};
    for (size_t i = 0; i + w <= n; ++i) {
        // For each usable lag: sub = window[::L]; mean; cumsum(centered);
        // r = z.range; s = ddof-0 std; tau = r/s when s>0 else 0;
        // log_tau = log(max(tau, 1e-12)); accumulate OLS terms.
        double count = 0.0, sum_x = 0.0, sum_y = 0.0, sum_xx = 0.0, sum_xy = 0.0;
        for (int li = 0; li < 4; ++li) {
            size_t L = (size_t)lag[li];
            if (L >= w / 2) continue;
            size_t m = (w + L - 1) / L;  // len(sub) = ceil(w / L)
            double mean = 0.0;
            for (size_t k = 0; k < m; ++k) mean += a[i + k * L];
            mean /= (double)m;
            double zmin = 0.0, zmax = 0.0, zcum = 0.0, sumsq = 0.0;
            for (size_t k = 0; k < m; ++k) {
                double yk = a[i + k * L] - mean;
                zcum += yk;
                if (zcum < zmin) zmin = zcum;
                if (zcum > zmax) zmax = zcum;
                sumsq += yk * yk;
            }
            double r = zmax - zmin;
            double s = std::sqrt(sumsq / (double)m);
            double tau = (s > 0.0) ? (r / s) : 0.0;
            double logtau = std::log(tau > 1e-12 ? tau : 1e-12);
            count += 1.0;
            sum_x += log_lag[li];
            sum_y += logtau;
            sum_xx += log_lag[li] * log_lag[li];
            sum_xy += log_lag[li] * logtau;
        }
        double denom = count * sum_xx - sum_x * sum_x;
        double slope = ((count >= 2.0) && (std::fabs(denom) > 1e-18))
                           ? (count * sum_xy - sum_x * sum_y) / denom
                           : 0.5;
        res[w - 1 + i] = slope;
    }
    return make_out(out);
}

// fractal_dimension — mirrors _fractal_dimension: per sliding window,
// normalize by (w - w_min)/span (0 when span == 0), count sign changes in the
// first difference, fd = 1 + changes/window. Arithmetic is deterministic
// (division/compare in the same order as numpy) so this is bit-stable.
nd1 fractal_dimension(nd1 a_in, int64_t window) {
    size_t n = a_in.shape(0);
    auto* out = new std::vector<double>(n, std::nan(""));
    const double* a = a_in.data();
    double* res = out->data();
    size_t w = (size_t)window;
    if (n < w) return make_out(out);
    for (size_t i = 0; i + w <= n; ++i) {
        double mn = a[i], mx = a[i];
        for (size_t k = 1; k < w; ++k) {
            double v = a[i + k];
            if (v < mn) mn = v;
            if (v > mx) mx = v;
        }
        double span = mx - mn;
        int64_t changes = 0;
        double prev_sign = std::nan("");
        for (size_t j = 0; j + 1 < w; ++j) {
            double nj = (span > 0.0) ? (a[i + j] - mn) / span : 0.0;
            double nj1 = (span > 0.0) ? (a[i + j + 1] - mn) / span : 0.0;
            double d1 = nj1 - nj;
            double sg = (d1 > 0.0) ? 1.0 : ((d1 < 0.0) ? -1.0 : 0.0);
            if (j > 0 && sg != prev_sign) ++changes;
            prev_sign = sg;
        }
        res[w - 1 + i] = 1.0 + (double)changes / (double)w;
    }
    return make_out(out);
}

// microstructure — mirrors _microstructure: all 8 output columns in the
// same order ((amihud_illiq, amihud_smooth, kyle_lambda, vpin_proxy,
// signed_vol, micro_clv, micro_vol_chg, micro_illiq)). Replicates the pandas
// rolling semantics used by the original: position-windowed, NaN-excluding
// mean with min_periods == window, ddof=1 std/var/cov, and pairwise-count
// gating for rolling cov.
std::tuple<nd1, nd1, nd1, nd1, nd1, nd1, nd1, nd1> microstructure(
    nd1 high_in, nd1 low_in, nd1 close_in, nd1 volume_in, int64_t window) {
    size_t n = close_in.shape(0);
    size_t w = ((size_t)window < 1) ? 1 : (size_t)window;
    const double* H = high_in.data();
    const double* L = low_in.data();
    const double* C = close_in.data();
    const double* V = volume_in.data();

    auto* amihud_v = new std::vector<double>(n, std::nan(""));
    auto* smooth_v = new std::vector<double>(n, std::nan(""));
    auto* kyle_v = new std::vector<double>(n, std::nan(""));
    auto* vpin_v = new std::vector<double>(n, std::nan(""));
    auto* signed_v = new std::vector<double>(n, std::nan(""));
    auto* clv_v = new std::vector<double>(n, 0.0);
    auto* volchg_v = new std::vector<double>(n, 0.0);
    auto* illiq_v = new std::vector<double>(n, 0.0);

    double* amihud = amihud_v->data();
    double* smooth = smooth_v->data();
    double* kyle = kyle_v->data();
    double* vpin_proxy = vpin_v->data();
    double* signed_vol = signed_v->data();
    double* micro_clv = clv_v->data();
    double* micro_vol_chg = volchg_v->data();
    double* micro_illiq = illiq_v->data();

    std::vector<double> ret(n, 0.0);       // close-to-close returns
    std::vector<double> pdiff(n, 0.0);     // close diffs
    std::vector<double> pstd(n, std::nan(""));
    std::vector<double> vpin_ratios(n, std::nan(""));

    for (size_t i = 0; i < n; ++i) {
        double hi = H[i], lo = L[i], c = C[i], v = V[i];
        double rg = ((hi - lo) > 1e-9) ? (hi - lo) : 1e-9;  // max(HL, 1e-9)
        micro_clv[i] = (c - lo) / rg;
        micro_illiq[i] = rg / ((v > 0.0) ? v : 1.0);
        if (i > 0) {
            double vc = (V[i] > 0.0) ? V[i] : 1.0;
            double vp = (V[i - 1] > 0.0) ? V[i - 1] : 1.0;
            micro_vol_chg[i] = std::log(vc / vp);
            ret[i] = (c - C[i - 1]) / C[i - 1];
            pdiff[i] = c - C[i - 1];
        }
        double dvol = c * v;
        if (dvol > 0.0) amihud[i] = std::fabs(ret[i]) / dvol;
    }

    // Per-position std-of-diffs + amihud smoothing (independent of signed_vol).
    for (size_t i = w - 1; i < n; ++i) {
        // price_diff rolling std, ddof=1 (all finite).
        double mp = 0.0;
        for (size_t jj = 0; jj < w; ++jj) mp += pdiff[i - w + 1 + jj];
        mp /= (double)w;
        double ss = 0.0;
        for (size_t jj = 0; jj < w; ++jj) {
            double d = pdiff[i - w + 1 + jj] - mp;
            ss += d * d;
        }
        pstd[i] = std::sqrt(ss / (double)(w - 1));

        // NaN-excluding rolling mean of amihud, min_periods == window.
        double sa = 0.0;
        size_t ca = 0;
        for (size_t jj = 0; jj < w; ++jj) {
            double x = amihud[i - w + 1 + jj];
            if (std::isnan(x)) continue;
            sa += x;
            ++ca;
        }
        if (ca >= w) smooth[i] = sa / (double)ca;
    }

    // signed_vol (z-score flow, NaN for i < w) then vpin_ratios.
    for (size_t i = w; i < n; ++i) {
        double z = (pstd[i] != 0.0) ? (pdiff[i] / pstd[i]) : 0.0;
        double bf = 1.0 / (1.0 + std::exp(-1.7 * z));
        signed_vol[i] = V[i] * (2.0 * bf - 1.0);
        if (V[i] > 0.0) vpin_ratios[i] = std::fabs(signed_vol[i]) / V[i];
    }

    // NaN-excluding rolling means of vpin_ratios, min_periods == window.
    for (size_t i = w - 1; i < n; ++i) {
        double svv = 0.0;
        size_t cvv = 0;
        for (size_t jj = 0; jj < w; ++jj) {
            double x = vpin_ratios[i - w + 1 + jj];
            if (std::isnan(x)) continue;
            svv += x;
            ++cvv;
        }
        if (cvv >= w) vpin_proxy[i] = svv / (double)cvv;
    }

    // kyle = rolling cov(returns, signed_vol) / rolling var(signed_vol), ddof=1,
    // pairwise-count-gated; var == 0 replaced by NaN (matches .replace(0, nan)).
    for (size_t i = w - 1; i < n; ++i) {
        size_t cnt = 0;
        double mx = 0.0, my = 0.0;
        for (size_t jj = 0; jj < w; ++jj) {
            double yv = signed_vol[i - w + 1 + jj];
            if (std::isnan(yv)) continue;
            mx += ret[i - w + 1 + jj];
            my += yv;
            ++cnt;
        }
        if (cnt < w || cnt < 2) continue;
        mx /= (double)cnt;
        my /= (double)cnt;
        double cov = 0.0, var_y = 0.0;
        for (size_t jj = 0; jj < w; ++jj) {
            double yv = signed_vol[i - w + 1 + jj];
            if (std::isnan(yv)) continue;
            double dx = ret[i - w + 1 + jj] - mx;
            double dy = yv - my;
            cov += dx * dy;
            var_y += dy * dy;
        }
        double denom = (double)(cnt - 1);
        cov /= denom;
        var_y /= denom;
        if (var_y != 0.0) kyle[i] = cov / var_y;
        // var_y == 0.0 -> replace(0, nan) -> kyle stays NaN.
    }

    return std::make_tuple(make_out(amihud_v), make_out(smooth_v),
                           make_out(kyle_v), make_out(vpin_v),
                           make_out(signed_v), make_out(clv_v),
                           make_out(volchg_v), make_out(illiq_v));
}

// ─────────────────────────────────────────────────────────────────────────────
// N2 sliding-window primitives — full-frame latency batch.
// Mirrors indicators.py helpers by replicating the pandas rolling semantics
// (position-windowed, NaN-skipping, min_periods == window) with the same
// tolerance contract as N1 (allclose 1e-9/1e-12).
// ─────────────────────────────────────────────────────────────────────────────

// rolling_rank_pct: Series.rolling(w).rank(pct=True) — average-rank percentile
// of the LAST value of each window, NaN-excluding.
nd1 rolling_rank_pct(nd1 a_in, int64_t window) {
    size_t n = a_in.shape(0);
    auto* out = new std::vector<double>(n, std::nan(""));
    const double* a = a_in.data();
    double* r = out->data();
    size_t w = (size_t)window;
    if (w < 1) return make_out(out);
    for (size_t i = w - 1; i < n; ++i) {
        double x = a[i];
        if (std::isnan(x)) continue;
        size_t count = 0, less = 0, eq = 0;
        for (size_t j = i + 1 - w; j <= i; ++j) {
            double v = a[j];
            if (std::isnan(v)) continue;
            ++count;
            if (v < x) ++less;
            else if (v == x) ++eq;
        }
        if (count < w) continue;  // pandas min_periods on the non-NaN count
        double rank = (double)less + (double)(eq + 1) / 2.0;
        r[i] = rank / (double)count;
    }
    return make_out(out);
}

// Rolling aggregates with NaN-skip and min_periods == window (pandas
// rolling(sum|mean|max|min) semantics; result NaN when non-NaN count < w).
nd1 rolling_sum(nd1 a_in, int64_t window) {
    size_t n = a_in.shape(0);
    auto* out = new std::vector<double>(n, std::nan(""));
    const double* a = a_in.data();
    double* r = out->data();
    size_t w = (size_t)window;
    if (w < 1) return make_out(out);
    for (size_t i = w - 1; i < n; ++i) {
        double s = 0.0;
        size_t cnt = 0;
        for (size_t j = i + 1 - w; j <= i; ++j) {
            double v = a[j];
            if (std::isnan(v)) continue;
            s += v;
            ++cnt;
        }
        if (cnt >= w) r[i] = s;
    }
    return make_out(out);
}

nd1 rolling_mean(nd1 a_in, int64_t window) {
    size_t n = a_in.shape(0);
    auto* out = new std::vector<double>(n, std::nan(""));
    const double* a = a_in.data();
    double* r = out->data();
    size_t w = (size_t)window;
    if (w < 1) return make_out(out);
    for (size_t i = w - 1; i < n; ++i) {
        double s = 0.0;
        size_t cnt = 0;
        for (size_t j = i + 1 - w; j <= i; ++j) {
            double v = a[j];
            if (std::isnan(v)) continue;
            s += v;
            ++cnt;
        }
        if (cnt >= w) r[i] = s / (double)cnt;
    }
    return make_out(out);
}

nd1 sliding_max(nd1 a_in, int64_t window) {
    size_t n = a_in.shape(0);
    auto* out = new std::vector<double>(n, std::nan(""));
    const double* a = a_in.data();
    double* r = out->data();
    size_t w = (size_t)window;
    if (w < 1) return make_out(out);
    for (size_t i = w - 1; i < n; ++i) {
        double m = -std::numeric_limits<double>::infinity();
        size_t cnt = 0;
        for (size_t j = i + 1 - w; j <= i; ++j) {
            double v = a[j];
            if (std::isnan(v)) continue;
            if (v > m) m = v;
            ++cnt;
        }
        if (cnt >= w) r[i] = m;
    }
    return make_out(out);
}

nd1 sliding_min(nd1 a_in, int64_t window) {
    size_t n = a_in.shape(0);
    auto* out = new std::vector<double>(n, std::nan(""));
    const double* a = a_in.data();
    double* r = out->data();
    size_t w = (size_t)window;
    if (w < 1) return make_out(out);
    for (size_t i = w - 1; i < n; ++i) {
        double m = std::numeric_limits<double>::infinity();
        size_t cnt = 0;
        for (size_t j = i + 1 - w; j <= i; ++j) {
            double v = a[j];
            if (std::isnan(v)) continue;
            if (v < m) m = v;
            ++cnt;
        }
        if (cnt >= w) r[i] = m;
    }
    return make_out(out);
}

// rolling_skew / rolling_kurt: replicate pandas nanops.nanskew / nankurt
// (adjusted Fisher-Pearson G1 / G2) per fixed window with min_periods==window.
nd1 rolling_skew(nd1 a_in, int64_t window) {
    size_t n = a_in.shape(0);
    auto* out = new std::vector<double>(n, std::nan(""));
    const double* a = a_in.data();
    double* r = out->data();
    size_t w = (size_t)window;
    if (w < 1) return make_out(out);
    for (size_t i = w - 1; i < n; ++i) {
        size_t cnt = 0;
        double sum = 0.0;
        for (size_t j = i + 1 - w; j <= i; ++j) {
            if (std::isnan(a[j])) continue;
            sum += a[j];
            ++cnt;
        }
        if (cnt < 3 || cnt < w) continue;  // min_periods + nanskew count<3
        double mean = sum / (double)cnt;
        double m2 = 0.0, m3 = 0.0;
        for (size_t j = i + 1 - w; j <= i; ++j) {
            if (std::isnan(a[j])) continue;
            double adj = a[j] - mean;
            double a2 = adj * adj;
            m2 += a2;
            m3 += a2 * adj;
        }
        if (std::fabs(m2) < 1e-14) m2 = 0.0;  // _zero_out_fperr
        if (std::fabs(m3) < 1e-14) m3 = 0.0;
        if (m2 == 0.0) {
            r[i] = 0.0;  // np.where(m2 == 0, 0, result) — matches nanskew & rolling skew
            continue;
        }
        double c = (double)cnt;
        r[i] = (c * std::sqrt(c - 1.0) / (c - 2.0)) * (m3 / std::pow(m2, 1.5));
    }
    return make_out(out);
}

nd1 rolling_kurt(nd1 a_in, int64_t window) {
    size_t n = a_in.shape(0);
    auto* out = new std::vector<double>(n, std::nan(""));
    const double* a = a_in.data();
    double* r = out->data();
    size_t w = (size_t)window;
    if (w < 1) return make_out(out);
    for (size_t i = w - 1; i < n; ++i) {
        size_t cnt = 0;
        double sum = 0.0;
        for (size_t j = i + 1 - w; j <= i; ++j) {
            if (std::isnan(a[j])) continue;
            sum += a[j];
            ++cnt;
        }
        if (cnt < 4 || cnt < w) continue;
        double mean = sum / (double)cnt;
        double m2 = 0.0, m4 = 0.0;
        for (size_t j = i + 1 - w; j <= i; ++j) {
            if (std::isnan(a[j])) continue;
            double adj = a[j] - mean;
            double a2 = adj * adj;
            m2 += a2;
            m4 += a2 * a2;
        }
        double c = (double)cnt;
        double adjk = 3.0 * (c - 1.0) * (c - 1.0) / ((c - 2.0) * (c - 3.0));
        double numerator = c * (c + 1.0) * (c - 1.0) * m4;
        double denominator = (c - 2.0) * (c - 3.0) * m2 * m2;
        if (std::fabs(numerator) < 1e-14) numerator = 0.0;
        if (std::fabs(denominator) < 1e-14) denominator = 0.0;
        if (denominator == 0.0) {
            // Degenerate all-equal window: pandas rolling kurt (windows.pyx
            // calc_kurt) yields exactly -3.0 for ANY n >= 4 here, while the
            // nanops guard would return 0. Empirically verified.
            r[i] = -3.0;
            continue;
        }
        r[i] = numerator / denominator - adjk;
    }
    return make_out(out);
}

// rolling_autocorr: Series.rolling(w).corr(Series.shift(lag)) — pairwise
// NaN-excluding Pearson corr with ddof=1 and pairwise count >= w gate.
nd1 rolling_autocorr(nd1 a_in, int64_t window, int64_t lag) {
    size_t n = a_in.shape(0);
    auto* out = new std::vector<double>(n, std::nan(""));
    const double* a = a_in.data();
    double* r = out->data();
    size_t w = (size_t)window;
    size_t L = (size_t)lag;
    if (w < 1) return make_out(out);
    for (size_t i = w - 1; i < n; ++i) {
        size_t cnt = 0;
        double sx = 0.0, sy = 0.0;
        for (size_t j = i + 1 - w; j <= i; ++j) {
            if (j < L) continue;
            double xv = a[j], yv = a[j - L];
            if (std::isnan(xv) || std::isnan(yv)) continue;
            sx += xv;
            sy += yv;
            ++cnt;
        }
        if (cnt < w || cnt < 2) continue;
        double mx = sx / (double)cnt;
        double my = sy / (double)cnt;
        double cov = 0.0, vx = 0.0, vy = 0.0;
        for (size_t j = i + 1 - w; j <= i; ++j) {
            if (j < L) continue;
            double xv = a[j], yv = a[j - L];
            if (std::isnan(xv) || std::isnan(yv)) continue;
            double dx = xv - mx;
            double dy = yv - my;
            cov += dx * dy;
            vx += dx * dx;
            vy += dy * dy;
        }
        double den = (double)(cnt - 1);
        cov /= den;
        vx /= den;
        vy /= den;
        if (vx == 0.0 || vy == 0.0) continue;
        r[i] = cov / std::sqrt(vx * vy);
    }
    return make_out(out);
}

NB_MODULE(vq_kernels, m) {
    m.def("adaptive_z_scores", &adaptive_z_scores, nb::arg("a"), nb::arg("sc"));
    m.def("kalman_filter", &kalman_filter, nb::arg("prices"), nb::arg("Q") = 1e-5,
          nb::arg("R") = 1e-3);
    m.def("fisher_core", &fisher_core, nb::arg("prices"), nb::arg("roll_min"),
          nb::arg("roll_max"), nb::arg("period"));
    m.def("hilbert_spectrum", &hilbert_spectrum, nb::arg("values"),
          nb::arg("window"));
    m.def("hurst_exponent", &hurst_exponent, nb::arg("a"),
          nb::arg("window"));
    m.def("fractal_dimension", &fractal_dimension, nb::arg("a"),
          nb::arg("window"));
    m.def("microstructure", &microstructure, nb::arg("high"), nb::arg("low"),
          nb::arg("close"), nb::arg("volume"), nb::arg("window"));
    m.def("rolling_rank_pct", &rolling_rank_pct, nb::arg("a"), nb::arg("window"));
    m.def("rolling_sum", &rolling_sum, nb::arg("a"), nb::arg("window"));
    m.def("rolling_mean", &rolling_mean, nb::arg("a"), nb::arg("window"));
    m.def("sliding_max", &sliding_max, nb::arg("a"), nb::arg("window"));
    m.def("sliding_min", &sliding_min, nb::arg("a"), nb::arg("window"));
    m.def("rolling_skew", &rolling_skew, nb::arg("a"), nb::arg("window"));
    m.def("rolling_kurt", &rolling_kurt, nb::arg("a"), nb::arg("window"));
    m.def("rolling_autocorr", &rolling_autocorr, nb::arg("a"), nb::arg("window"),
          nb::arg("lag"));
}