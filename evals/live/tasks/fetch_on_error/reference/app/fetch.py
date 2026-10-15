def fetch_all(urls, fetch, on_error=None):
    """Fetch each url with `fetch(url)`, in order.

    Errors propagate unless `on_error(url, exc)` is given; then it is called and the
    failed url is left out of the results.
    """
    results = []
    for url in urls:
        if on_error is None:
            results.append(fetch(url))
            continue
        try:
            results.append(fetch(url))
        except Exception as exc:
            on_error(url, exc)
    return results
