import asyncio

try:
    import focused_crawler as fc
except ImportError:
    from . import focused_crawler as fc


def test_build_focused_crawl_plan_prefers_modality_tree_and_blocks_noise():
    plan = fc.build_focused_crawl_plan(
        "https://www.siemens-healthineers.com/magnetic-resonance-imaging"
    )

    assert plan.start_url == "https://www.siemens-healthineers.com/magnetic-resonance-imaging"
    assert plan.modality == "mri"
    assert any("magnetic-resonance-imaging" in pattern for pattern in plan.include_patterns)
    assert any("mri" in pattern for pattern in plan.include_patterns)
    assert any("products" in pattern for pattern in plan.include_patterns)
    assert any("cookie" in pattern for pattern in plan.exclude_patterns)
    assert any("privacy" in pattern for pattern in plan.exclude_patterns)
    assert any("career" in pattern for pattern in plan.exclude_patterns)
    assert any("support" in pattern for pattern in plan.exclude_patterns)


def test_rank_focused_links_prioritizes_product_children_over_noise():
    start_url = "https://www.siemens-healthineers.com/magnetic-resonance-imaging"
    links = [
        "https://www.siemens-healthineers.com/cookie",
        "https://www.siemens-healthineers.com/magnetic-resonance-imaging/high-v-mri/magnetom-free-xl",
        "https://www.siemens-healthineers.com/investor-relations",
        "https://www.siemens-healthineers.com/magnetic-resonance-imaging/mri-guided-therapy",
        "https://www.siemens-healthineers.com/news/press-release",
    ]

    ranked = fc.rank_focused_links(links, start_url)

    assert ranked[0] == "https://www.siemens-healthineers.com/magnetic-resonance-imaging/high-v-mri/magnetom-free-xl"
    assert ranked[1] == "https://www.siemens-healthineers.com/magnetic-resonance-imaging/mri-guided-therapy"
    assert ranked[-1] in {
        "https://www.siemens-healthineers.com/cookie",
        "https://www.siemens-healthineers.com/investor-relations",
        "https://www.siemens-healthineers.com/news/press-release",
    }


def test_focused_crawl_wrapper_forwards_focused_filters():
    captured = {}

    async def fake_crawl(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return ["ok"]

    original = fc.crawl
    fc.crawl = fake_crawl
    try:
        result = asyncio.run(
            fc.crawl_focused_tree(
                "https://www.siemens-healthineers.com/magnetic-resonance-imaging",
                max_depth=3,
                max_pages=11,
                modality="mri",
                include_patterns=["*/magnetic-resonance-imaging/*"],
                exclude_patterns=["*press*"],
                concurrency=2,
                respect_robots=False,
                scraper="sentinel",
                verbose=False,
            )
        )
    finally:
        fc.crawl = original

    assert result == ["ok"]
    assert captured["args"][0] == "https://www.siemens-healthineers.com/magnetic-resonance-imaging"
    assert captured["kwargs"]["max_depth"] == 3
    assert captured["kwargs"]["max_pages"] == 11
    assert captured["kwargs"]["concurrency"] == 2
    assert captured["kwargs"]["respect_robots"] is False
    assert captured["kwargs"]["scraper"] == "sentinel"
    assert captured["kwargs"]["verbose"] is False
    assert "*/magnetic-resonance-imaging*" in captured["kwargs"]["include_patterns"]
    assert any("cookie" in pattern for pattern in captured["kwargs"]["exclude_patterns"])
    assert "*press*" in captured["kwargs"]["exclude_patterns"]
