def test_commoncrawl_reports_progress_per_host(monkeypatch, capsys):
    """It is the slowest source by an order of magnitude -- 3,057 seconds
    against 88 for the next worst -- and printed nothing while it ran. Fifty
    minutes of silence in CI is indistinguishable from a hung job, and was
    reported as one."""
    from argus.discovery import commoncrawl

    src = commoncrawl.CommonCrawlSource(
        hosts=["jobs.ashbyhq.com", "jobs.lever.co"], crawls=1, pause=0
    )
    monkeypatch.setattr(src, "_recent_crawls", lambda: ["CC-MAIN-2026-01"])
    monkeypatch.setattr(src, "_pages", lambda crawl, host: 0)
    list(src.discover())

    lines = [x for x in capsys.readouterr().err.splitlines() if x.startswith("commoncrawl")]
    assert len(lines) == 2, "one line per host, not one at the end"
    assert "jobs.ashbyhq.com" in lines[0] and "1/2" in lines[0]
    assert "jobs.lever.co" in lines[1] and "2/2" in lines[1]
