from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

import numpy as np
from dotenv import load_dotenv
from massive import RESTClient
from sklearn.feature_extraction.text import TfidfVectorizer
from textblob import TextBlob

logging.getLogger(__name__).addHandler(logging.NullHandler())
_log = logging.getLogger(__name__)

# Load environment variables (supports project-root .env)
_dotenv_path = Path(__file__).parent / ".." / ".env"
if _dotenv_path.exists():
    load_dotenv(dotenv_path=_dotenv_path)
else:
    _log.warning(
        ".env file not found at %s. Using default or environment-set credentials.",
        _dotenv_path,
    )


class NewsCache:
    """
    File-based cache for news articles and their embeddings.
    Stores news data as JSON files and embeddings as numpy arrays.
    """

    def __init__(self, cache_dir: str = "news_cache", verbose: bool = False):
        self.cache_dir = cache_dir
        self.verbose = verbose
        Path(cache_dir).mkdir(parents=True, exist_ok=True)

    def _get_cache_key(
        self, ticker: str, start_dt: datetime.datetime, end_dt: datetime.datetime
    ) -> str:
        """Generate a unique cache key for a ticker and date range."""
        start_str = start_dt.strftime("%Y%m%d")
        end_str = end_dt.strftime("%Y%m%d")
        key_str = f"{ticker}_{start_str}_{end_str}"
        return hashlib.md5(key_str.encode(), usedforsecurity=False).hexdigest()

    def _get_cache_paths(
        self, ticker: str, start_dt: datetime.datetime, end_dt: datetime.datetime
    ) -> tuple[str, str]:
        """Get the file paths for cached news data (articles JSON and embeddings NPY)."""
        cache_key = self._get_cache_key(ticker, start_dt, end_dt)
        ticker_dir = Path(self.cache_dir) / ticker.upper()
        ticker_dir.mkdir(parents=True, exist_ok=True)
        articles_path = str(ticker_dir / f"{cache_key}.json")
        embeddings_path = str(ticker_dir / f"{cache_key}_embeddings.npy")
        return articles_path, embeddings_path

    def get(
        self, ticker: str, start_dt: datetime.datetime, end_dt: datetime.datetime
    ) -> tuple[list[dict[str, Any]], np.ndarray | None] | None:
        """Retrieve cached news articles and embeddings if they exist."""
        articles_path, embeddings_path = self._get_cache_paths(ticker, start_dt, end_dt)

        if not Path(articles_path).exists():
            return None

        try:
            # Load articles
            with Path(articles_path).open() as f:
                data = json.load(f)
            articles = data["articles"]

            # Load embeddings if available
            embeddings = None
            if Path(embeddings_path).exists():
                embeddings = np.load(embeddings_path)

            if self.verbose:
                emb_info = (
                    f", embeddings shape {embeddings.shape}"
                    if embeddings is not None
                    else ""
                )
                print(
                    f"Cache HIT: {ticker} {start_dt.date()} to {end_dt.date()} ({len(articles)} articles{emb_info})"
                )

            return articles, embeddings
        except Exception as e:
            if self.verbose:
                print(f"Cache read error: {e}")
            return None

    def put(
        self,
        ticker: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
        articles: list[dict[str, Any]],
        embeddings: np.ndarray | None = None,
    ):
        """Store news articles and embeddings in cache."""
        articles_path, embeddings_path = self._get_cache_paths(ticker, start_dt, end_dt)

        try:
            # Save articles
            cache_data = {
                "ticker": ticker,
                "start_date": start_dt.isoformat(),
                "end_date": end_dt.isoformat(),
                "cached_at": datetime.datetime.now().isoformat(),
                "articles": articles,
                "has_embeddings": embeddings is not None,
            }

            with Path(articles_path).open("w") as f:
                json.dump(cache_data, f, indent=2)

            # Save embeddings if provided
            if embeddings is not None:
                np.save(embeddings_path, embeddings)

            if self.verbose:
                emb_info = (
                    f" with embeddings shape {embeddings.shape}"
                    if embeddings is not None
                    else ""
                )
                print(f"Cached {len(articles)} articles for {ticker}{emb_info}")
        except Exception as e:
            if self.verbose:
                print(f"Cache write error: {e}")

    def cleanup_old_entries(self, days_old: int = 7):
        """Remove cache entries older than specified days."""
        cutoff = datetime.datetime.now() - datetime.timedelta(days=days_old)
        removed = 0

        for root, _dirs, files in os.walk(self.cache_dir):
            for file in files:
                if not (file.endswith(".json") or file.endswith(".npy")):
                    continue

                filepath = Path(root) / file
                try:
                    mtime = datetime.datetime.fromtimestamp(filepath.stat().st_mtime)
                    if mtime < cutoff:
                        filepath.unlink()
                        removed += 1
                except Exception:
                    _log.debug(
                        "Could not clean up cache file %s", filepath, exc_info=True
                    )

        if self.verbose and removed > 0:
            print(f"Cleaned up {removed} old cache entries")


class NewsHandler:
    """
    Handles:
    - Massive REST client configuration for news API
    - News article fetching by ticker and date range
    - Persistent news caching via NewsCache
    - Text processing and embedding generation for ML
    """

    # Class-level locks/semaphores
    _net_sema = threading.Semaphore(20)
    _cache_write_lock = threading.RLock()

    def __init__(
        self,
        cache_dir: str = "news_cache",
        verbose: bool = False,
        http_pool_size: int = 20,
        retries: int = 3,
    ):
        self.massive_client = RESTClient(
            api_key=os.getenv("MASSIVE_API_KEY", ""),
            num_pools=http_pool_size,
            retries=retries,
        )
        self.news_cache = NewsCache(cache_dir=cache_dir, verbose=verbose)
        self.verbose = verbose

    def fetch_news(
        self,
        ticker: str,
        start_dt: datetime.datetime,
        end_dt: datetime.datetime,
        limit: int = 10,
        generate_embeddings: bool = False,
        tfidf_features: int = 50,
    ) -> tuple[list[dict[str, Any]], np.ndarray | None]:
        """
        Fetch news articles from Massive for a ticker and date range.
        Returns a tuple of (articles, embeddings).

        Args:
            ticker: Stock ticker symbol
            start_dt: Start datetime
            end_dt: End datetime
            limit: Maximum number of articles to fetch
            generate_embeddings: Whether to generate embeddings for the articles
            tfidf_features: Number of TF-IDF features if generating embeddings

        Returns:
            Tuple of (list of article dictionaries, optional embeddings array)
        """
        # Check cache first
        cached_data = self.news_cache.get(ticker, start_dt, end_dt)
        if cached_data is not None:
            articles, embeddings = cached_data
            # If embeddings requested but not cached, generate them
            if generate_embeddings and embeddings is None and articles:
                embeddings = self.create_embeddings(
                    articles, tfidf_features=tfidf_features
                )
                # Update cache with embeddings
                with NewsHandler._cache_write_lock:
                    self.news_cache.put(ticker, start_dt, end_dt, articles, embeddings)
            return articles, embeddings

        articles = []

        try:
            with NewsHandler._net_sema:
                # Format dates for Massive API
                start_str = start_dt.strftime("%Y-%m-%d")
                end_str = end_dt.strftime("%Y-%m-%d")

                # Fetch news using Massive client
                news_iter = self.massive_client.list_ticker_news(
                    ticker=ticker.upper(),
                    published_utc_gte=start_str,
                    published_utc_lte=end_str,
                    limit=limit,
                    order="asc",
                )

                for article in news_iter:
                    article_data = {
                        "id": article.id,  # type: ignore[union-attr]
                        "title": article.title,
                        "author": getattr(article, "author", ""),
                        "published_utc": article.published_utc,  # type: ignore[union-attr]
                        "article_url": article.article_url,  # type: ignore[union-attr]
                        "description": getattr(article, "description", ""),
                        "keywords": getattr(article, "keywords", []),
                        "publisher": getattr(article.publisher, "name", "")
                        if hasattr(article, "publisher")
                        else "",
                        "tickers": getattr(article, "tickers", [ticker]),
                    }
                    articles.append(article_data)

                if self.verbose:
                    print(
                        f"Fetched {len(articles)} articles for {ticker} from {start_str} to {end_str}"
                    )

        except Exception as e:
            if self.verbose:
                print(f"Error fetching news for {ticker}: {e}")
            articles = []

        # Generate embeddings if requested
        embeddings = None
        if generate_embeddings and articles:
            embeddings = self.create_embeddings(articles, tfidf_features=tfidf_features)

        # Cache the results
        with NewsHandler._cache_write_lock:
            self.news_cache.put(ticker, start_dt, end_dt, articles, embeddings)

        return articles, embeddings

    def preprocess_text(self, text: str) -> str:
        """
        Basic text preprocessing for embedding generation.
        """
        if not text:
            return ""

        # Convert to lowercase
        text = text.lower()

        # Remove extra whitespace
        text = " ".join(text.split())

        return text

    # TODO: can be expanded by adding more advanced NLP techniques (using other embedding models, LLMs etc.)
    def create_embeddings(
        self,
        articles: list[dict[str, Any]],
        tfidf_features: int = 50,
    ) -> np.ndarray:
        """
        Create embeddings using both TF-IDF and sentiment analysis.

        Args:
            articles: List of article dictionaries
            tfidf_features: Number of TF-IDF features to extract

        Returns:
            numpy array of shape (n_articles, tfidf_features + 3)
            The last 3 features are sentiment scores: [negative, neutral, positive]
        """
        if not articles:
            return np.array([])

        # Combine title and description for each article
        texts = []
        for article in articles:
            title = article.get("title", "")
            desc = article.get("description", "")
            text = f"{title} {desc}"
            texts.append(self.preprocess_text(text))

        # Create TF-IDF embeddings
        vectorizer = TfidfVectorizer(max_features=tfidf_features, stop_words="english")
        tfidf_emb = vectorizer.fit_transform(texts).toarray()

        # Create sentiment features
        sentiment_features = []
        for text in texts:
            if not text.strip():
                sentiment_features.append([0.0, 1.0, 0.0])  # neutral default
                continue

            # Analyze sentiment using TextBlob
            blob = TextBlob(text)
            polarity = blob.sentiment.polarity  # -1 (negative) to 1 (positive)

            # Convert to negative, neutral, positive scores
            if polarity < -0.1:
                scores = [abs(polarity), 0.0, 0.0]
            elif polarity > 0.1:
                scores = [0.0, 0.0, polarity]
            else:
                scores = [0.0, 1.0, 0.0]

            sentiment_features.append(scores)

        sentiment_emb = np.array(sentiment_features)

        # Combine embeddings
        combined = np.hstack([tfidf_emb, sentiment_emb])

        if self.verbose:
            print(
                f"Created embeddings: shape {combined.shape} (TF-IDF: {tfidf_features}, Sentiment: 3)"
            )

        return combined

    # Cache maintenance helpers
    def cleanup_cache(self, days_old: int = 7):
        """Remove cache entries older than specified days."""
        self.news_cache.cleanup_old_entries(days_old)
