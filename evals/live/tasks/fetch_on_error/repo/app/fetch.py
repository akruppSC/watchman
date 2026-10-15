def fetch_all(urls, fetch):
    """Fetch each url with `fetch(url)`, in order. Errors propagate."""
    results = []
    for url in urls:
        results.append(fetch(url))
    return results
