try:
    from .preprocessing import preprocess_page_result
    from .scraper import PageResult
except ImportError:
    from preprocessing import preprocess_page_result
    from scraper import PageResult


def test_preprocessing_drops_cookie_noise_and_keeps_product_content():
    page = PageResult(
        url="https://www.siemens-healthineers.com/magnetic-resonance-imaging",
        title="MAGNETOM Flow MRI system",
        markdown="""
        | CookieConsent | Stores the user's cookie consent state for the current domain | 1 year | HTTP Cookie |
        | locale | The cookie determines the preferred language and country-setting of the visitor | Persistent | HTML Local Storage |
        | PHPSESSID | Preserves user session state across page requests. | Session | HTTP Cookie |

        MAGNETOM Flow
        Magnetic resonance imaging system
        1.5 Tesla
        70 cm bore
        AI workflow
        Request quote
        """,
        success=True,
    )

    result = preprocess_page_result(page)

    assert "MAGNETOM Flow" in result.clean_text
    assert "1.5 Tesla" in result.clean_text
    assert "70 cm bore" in result.clean_text
    assert "CookieConsent" not in result.clean_text
    assert "HTTP Cookie" not in result.clean_text
    assert result.useful_block_count >= 1
    assert result.dropped_block_count >= 1
    assert result.page_type in {"product_detail", "product_category"}
    assert result.source_success is True
    assert result.source_error is None
    assert result.source_metadata == {}


def test_preprocessing_salvages_useful_lines_from_mixed_block():
    page = PageResult(
        url="https://www.siemens-healthineers.com/computed-tomography",
        title="SOMATOM X.cite CT",
        markdown="""
        | CookieConsent | Stores the user's cookie consent state for the current domain | HTTP Cookie |
        SOMATOM X.cite
        Computed tomography scanner
        128 slice
        AI workflow
        Contact sales
        auth0 session state
        """,
        success=True,
    )

    result = preprocess_page_result(page)

    assert "SOMATOM X.cite" in result.clean_text
    assert "128 slice" in result.clean_text
    assert "CookieConsent" not in result.clean_text
    assert "auth0 session state" not in result.clean_text
    assert result.useful_block_count >= 1


def test_preprocessing_returns_empty_text_for_noise_only_input():
    page = PageResult(
        url="https://www.siemens-healthineers.com/privacy",
        title="Privacy and cookies",
        markdown="""
        | CookieConsent | Stores the user's cookie consent state for the current domain | Session | HTTP Cookie |
        | PHPSESSID | Preserves user session state across page requests. | Session | HTTP Cookie |
        Accept all cookies
        Manage preferences
        """,
        success=True,
    )

    result = preprocess_page_result(page)

    assert result.clean_text == ""
    assert result.useful_block_count == 0
    assert result.dropped_block_count >= 1
    assert result.page_type == "legal_noise"


def test_preprocessing_drops_fallback_navigation_page():
    page = PageResult(
        url="https://www.siemens-healthineers.com/us/missing-product",
        title="Siemens Healthineers",
        markdown="""
        Help us to improve our Website
        If a link does not work, please let us know.
        Possible reasons page was not found:
        We moved the page or deleted it
        Check if URL address is correct
        Alternate recommendations:
        Products & Services
        Home
        Search
        Contact Us
        """,
        success=True,
        metadata={"source_quality": "low_confidence"},
    )

    result = preprocess_page_result(page)

    assert result.clean_text == ""
    assert result.useful_block_count == 0
    assert "fallback_page" in result.dropped_reasons
    assert result.page_type == "fallback_noise"


def test_preprocessing_drops_fallback_when_navigation_is_split_from_error_text():
    page = PageResult(
        url="https://www.siemens-healthineers.com/us/missing-product",
        title="Siemens Healthineers",
        markdown="""
        Help us to improve our Website

        Possible reasons page was not found:

        Products & Services

        Home Products & Services Search
        """,
        success=True,
    )

    result = preprocess_page_result(page)

    assert result.clean_text == ""
    assert result.page_type == "fallback_noise"


def test_preprocessing_does_not_classify_product_copy_with_support_word_as_support_noise():
    page = PageResult(
        url="https://www.example.com/products/computed-tomography",
        title="Computed tomography product",
        markdown="""
        Advanced CT scanner platform
        Remote services support system uptime and workflow.
        82 cm bore
        """,
        success=True,
    )

    result = preprocess_page_result(page)

    assert result.page_type in {"product_detail", "product_category"}
    assert "Remote services support" in result.clean_text


def test_preprocessing_drops_geo_location_banner_but_keeps_product_list():
    page = PageResult(
        url="https://www.example.com/products/computed-tomography",
        title="Revolution Family",
        markdown="""
        Select another country or region to view content specific to your location.

        Revolution Apex
        Computed tomography scanner
        82 cm bore
        High throughput workflow
        """,
        success=True,
    )

    result = preprocess_page_result(page)

    assert "Select another country" not in result.clean_text
    assert "Revolution Apex" in result.clean_text
    assert "82 cm bore" in result.clean_text


def test_preprocessing_dedupes_duplicate_normalized_blocks():
    page = PageResult(
        url="https://www.siemens-healthineers.com/magnetic-resonance-imaging",
        title="MAGNETOM Flow MRI system",
        markdown="""
        MAGNETOM Flow
        Magnetic resonance imaging system
        1.5 Tesla

        MAGNETOM   Flow
        Magnetic resonance imaging system
        1.5   Tesla
        """,
        success=True,
    )

    result = preprocess_page_result(page)

    assert result.clean_text.count("MAGNETOM Flow") == 1
    assert result.clean_text.count("1.5 Tesla") == 1
    assert "duplicate_block" in result.dropped_reasons or result.dropped_block_count >= 1


def test_preprocessing_salvages_useful_content_from_mixed_noise():
    page = PageResult(
        url="https://www.siemens-healthineers.com/computed-tomography",
        title="SOMATOM X.cite CT",
        markdown="""
        | CookieConsent | Stores the user's cookie consent state for the current domain | HTTP Cookie |
        SOMATOM X.cite
        Computed tomography scanner
        128 slice
        AI workflow
        Contact sales
        auth0 session state
        """,
        success=False,
        error="upstream fetch timed out",
        metadata={"source": "crawler", "attempt": 2},
    )

    result = preprocess_page_result(page)

    assert "SOMATOM X.cite" in result.clean_text
    assert "128 slice" in result.clean_text
    assert "CookieConsent" not in result.clean_text
    assert "auth0 session state" not in result.clean_text
    assert result.source_success is False
    assert result.source_error == "upstream fetch timed out"
    assert result.source_metadata == {"source": "crawler", "attempt": 2}


def test_preprocessing_keeps_legitimate_non_english_product_content():
    page = PageResult(
        url="https://www.siemens-healthineers.com/de-de/magnetom",
        title="MAGNETOM Flow",
        markdown="""
        MAGNETOM Flow
        Hochmodernes MRI-System
        1.5 Tesla
        70 cm bore
        Nachhaltige Bildgebung fuer den klinischen Alltag
        """,
        success=True,
    )

    result = preprocess_page_result(page)

    assert "MAGNETOM Flow" in result.clean_text
    assert "Hochmodernes MRI-System" in result.clean_text
    assert "1.5 Tesla" in result.clean_text
    assert "70 cm bore" in result.clean_text
    assert result.useful_block_count >= 1


def test_preprocessing_drops_non_english_news_lines_for_us_focus():
    page = PageResult(
        url="https://www.siemens-healthineers.com/en-us",
        title="Siemens Healthineers",
        markdown="""
        Nachhaltige Transformation im Gesundheitswesen
        Aktuelle News & Kundengeschichten
        MAGNETOM Flow
        Magnetic resonance imaging system
        1.5 Tesla
        """,
        success=True,
    )

    result = preprocess_page_result(page)

    assert "MAGNETOM Flow" in result.clean_text
    assert "1.5 Tesla" in result.clean_text
    assert result.useful_block_count >= 1


def test_preprocessing_normalizes_unicode_spacing_before_quality_checks():
    page = PageResult(
        url="https://www.siemens-healthineers.com/en-us/magnetic-resonance-imaging",
        title="MAGNETOM Flow",
        markdown="MAGNETOM\u00a0Flow\u200b\nMagnetic resonance imaging system\n1.5\u00a0Tesla\n70 cm bore",
        success=True,
    )

    result = preprocess_page_result(page)

    assert "MAGNETOM Flow" in result.clean_text
    assert "1.5 Tesla" in result.clean_text
