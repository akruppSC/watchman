def fetch_all(urls, fetch, on_error=None):
    results = []
    for url in urls:
        results.append(fetch(url))
    return results
