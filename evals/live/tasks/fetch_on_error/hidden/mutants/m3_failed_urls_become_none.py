def fetch_all(urls, fetch, on_error=None):
    results = []
    for url in urls:
        if on_error is None:
            results.append(fetch(url))
            continue
        try:
            results.append(fetch(url))
        except Exception as exc:
            on_error(url, exc)
            results.append(None)
    return results
