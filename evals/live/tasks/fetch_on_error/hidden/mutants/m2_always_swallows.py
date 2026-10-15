def fetch_all(urls, fetch, on_error=None):
    results = []
    for url in urls:
        try:
            results.append(fetch(url))
        except Exception as exc:
            if on_error:
                on_error(url, exc)
    return results
