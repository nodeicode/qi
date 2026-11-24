"""
Constants for Named Vector Exit Logic.
Used to index into the 'params' array passed to Numba functions.
"""

# Exit Parameter Indices
EXIT_STOP_LOSS = 0
EXIT_TAKE_PROFIT = 1
EXIT_USE_TRAIL = 2
EXIT_MAX_HOLD = 3
EXIT_BREAKEVEN = 4
EXIT_IS_SPREAD = 5
EXIT_DIRECTION = 6

# Total count for array initialization
EXIT_PARAMS_COUNT = 7

# IC-specific exit_params indices
IC_K_SP = 7  # Short put strike
IC_K_SC = 8  # Short call strike
IC_K_LP = 9  # Long put strike
IC_K_LC = 10  # Long call strike
IC_ENTRY_ATR = 11  # ATR at entry for breach calculation
IC_PARAMS_COUNT = 12
