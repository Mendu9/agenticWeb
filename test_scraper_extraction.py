try:
    from .scraper import _assess_source_quality, _build_retry_candidates, _canonicalize_to_us_url, html_to_markdown
except ImportError:
    from scraper import _assess_source_quality, _build_retry_candidates, _canonicalize_to_us_url, html_to_markdown


def test_html_to_markdown_prefers_semantic_content_over_cookie_table():
    html = """
    <html>
      <body>
        <main>
          <table>
            <tr><td>CookieConsent</td><td>Stores the user's cookie consent state</td><td>HTTP Cookie</td></tr>
            <tr><td>PHPSESSID</td><td>Preserves user session state</td><td>Session</td></tr>
          </table>
          <section>
            <h1>MAGNETOM Flow</h1>
            <p>Magnetic resonance imaging system for modern workflow.</p>
            <p>1.5 Tesla with a 70 cm bore.</p>
          </section>
        </main>
      </body>
    </html>
    """

    markdown = html_to_markdown(html, "https://www.siemens-healthineers.com/magnetic-resonance-imaging")

    assert "MAGNETOM Flow" in markdown
    assert "1.5 Tesla" in markdown
    assert "CookieConsent" not in markdown
    assert "HTTP Cookie" not in markdown


def test_html_to_markdown_keeps_card_style_product_entries():
    html = """
    <html>
      <body>
        <main>
          <section>
            <h2>High-V MRI</h2>
            <p>High-V MRI combines the power of digitalization with a new field strength of 0.55T.</p>
            <div>
              <a href="/magnetic-resonance-imaging/high-v-mri/magnetom-free-xl">
                <div>MAGNETOM Free.XL</div>
                <div>Leading the way</div>
              </a>
            </div>
            <div>
              <a href="/magnetic-resonance-imaging/high-v-mri/magnetom-free-max">
                <div>MAGNETOM Free.Max</div>
                <div>Breaking barriers</div>
              </a>
            </div>
          </section>
        </main>
      </body>
    </html>
    """

    markdown = html_to_markdown(html, "https://www.siemens-healthineers.com/magnetic-resonance-imaging")

    assert "High-V MRI" in markdown
    assert "MAGNETOM Free.XL" in markdown
    assert "MAGNETOM Free.Max" in markdown


def test_source_quality_marks_geo_cookie_page_as_bad_source():
    quality = _assess_source_quality(
        url="https://www.siemens-healthineers.com/de",
        title="Startseite",
        markdown="CookieConsent HTTP Cookie privacy policy session state",
        links=[
            "https://www.siemens-healthineers.com/cookie",
            "https://www.cookiebot.com/goto/privacy-policy/",
            "https://privacy.microsoft.com/en-us/PrivacyStatement",
            "https://auth0.com/privacy",
            "https://www.walkme.com/privacy-policy/",
        ],
        block_signals=["geo_redirect"],
    )

    assert quality["source_quality"] == "bad_source_page"
    assert "geo_redirect" in quality["bad_source_reasons"]
    assert "mostly_noise_links" in quality["bad_source_reasons"]


def test_retry_candidates_include_locale_rewrites_and_homepage():
    candidates = _build_retry_candidates(
        "https://www.siemens-healthineers.com/de/products-services",
        ["https://www.siemens-healthineers.com/en/magnetic-resonance-imaging"],
    )

    assert any("/en-us/products-services" in candidate or "/en/products-services" in candidate for candidate in candidates)
    assert "https://www.siemens-healthineers.com/" in candidates


def test_canonicalize_to_us_url_prefers_en_us():
    assert _canonicalize_to_us_url("https://www.siemens-healthineers.com/de") == "https://www.siemens-healthineers.com/en-us"
    assert _canonicalize_to_us_url("https://www.siemens-healthineers.com/de/products-services") == "https://www.siemens-healthineers.com/en-us/products-services"
