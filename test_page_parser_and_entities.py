try:
    from .entity_extractor import extract_product_records
    from .page_parser import parse_structured_page
except ImportError:
    from entity_extractor import extract_product_records
    from page_parser import parse_structured_page


def test_structured_parser_and_entity_extractor_keep_clinical_parameters():
    html = """
    <html>
      <body>
        <main>
          <section>
            <h1>BIOGRAPH One - transform PET/MR image quality for a new era</h1>
            <p>Discover the future of personalized imaging with BIOGRAPH One.</p>
            <p>Injected dose: 184 MBq i.v.</p>
            <p>Uptake time: 79 min p.i.</p>
            <p>Sequences: MRAC (VIBE-DIXON), DWI</p>
            <p>MR TA: 5:40 min (1:25 min per bed)</p>
            <p>PET TA: 20 min (5 min per bed)</p>
            <p>Total scan range: 110 cm (4 beds)</p>
          </section>
        </main>
      </body>
    </html>
    """

    page = parse_structured_page(
        html,
        url="https://www.siemens-healthineers.com/magnetic-resonance-imaging/pet-mr-scanner",
        title="BIOGRAPH One - Siemens Healthineers",
    )
    records = extract_product_records(page)

    assert any("BIOGRAPH One" in block.text for block in page.blocks)
    assert records
    record = records[0]
    assert record.product_name.startswith("BIOGRAPH One")
    assert "injected_dose" in record.technical_specs
    assert "184 MBq i.v." in record.technical_specs["injected_dose"]
    assert "uptake_time" in record.technical_specs
    assert "79 min p.i." in record.technical_specs["uptake_time"]
    assert "sequences" in record.technical_specs
    assert "MRAC (VIBE-DIXON), DWI" in record.technical_specs["sequences"]
    assert "mr_ta" in record.technical_specs
    assert "pet_ta" in record.technical_specs
    assert "scan_range" in record.technical_specs


def test_parser_keeps_duplicate_sections_card_blocks_and_provenance():
    html = """
    <html>
      <body>
        <main>
          <section id="first">
            <h2>MAGNETOM Free.Max</h2>
            <p>1.5 Tesla with a 70 cm bore.</p>
            <div>
              <a href="/products/free-max">
                <div>MAGNETOM Free.Max</div>
                <div>Learn more</div>
              </a>
            </div>
          </section>
          <section id="second">
            <h2>MAGNETOM Free.Max</h2>
            <p>1.5 Tesla with a 70 cm bore.</p>
            <div>
              <a href="/products/free-max">
                <div>MAGNETOM Free.Max</div>
                <div>Learn more</div>
              </a>
            </div>
          </section>
          <div class="spec">
            <p>Injected dose: 184 MBq i.v.</p>
          </div>
        </main>
      </body>
    </html>
    """

    page = parse_structured_page(
        html,
        url="https://www.example.com/products",
        title="Product page",
    )

    card_blocks = [block for block in page.blocks if block.block_type == "card"]
    assert len(card_blocks) >= 2
    assert any(block.text == "Injected dose: 184 MBq i.v." for block in page.blocks)
    assert all(block.provenance.get("source_path") for block in page.blocks)
    duplicate_cards = [block for block in card_blocks if block.heading == "MAGNETOM Free.Max"]
    assert len(duplicate_cards) >= 2
    assert duplicate_cards[0].provenance["source_path"] != duplicate_cards[1].provenance["source_path"]


def test_entity_extractor_merges_card_and_spec_evidence():
    html = """
    <html>
      <body>
        <main>
          <section>
            <h2>MAGNETOM Free.Max</h2>
            <p>1.5 Tesla with a 70 cm bore.</p>
            <div>
              <a href="/products/free-max">
                <div>MAGNETOM Free.Max</div>
                <div>Learn more</div>
              </a>
            </div>
          </section>
          <div class="spec">
            <p>Injected dose: 184 MBq i.v.</p>
          </div>
        </main>
      </body>
    </html>
    """

    page = parse_structured_page(
        html,
        url="https://www.example.com/products",
        title="MAGNETOM Free.Max - Example",
    )
    records = extract_product_records(page)

    assert records
    record = records[0]
    assert record.product_name == "MAGNETOM Free.Max"
    assert "field_strength" in record.technical_specs
    assert "injected_dose" in record.technical_specs
    assert any(block_id.startswith("card_") for block_id in record.evidence_block_ids)
    assert any(block_id.startswith("text_") or block_id.startswith("section_") for block_id in record.evidence_block_ids)
