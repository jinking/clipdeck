from __future__ import annotations

from clipdeck.ingestion.metadata import extract_identifiers


def test_extract_identifiers_empty_and_none() -> None:
    assert extract_identifiers("") == []
    assert extract_identifiers("   ") == []


def test_extract_academic_identifiers_comprehensive() -> None:
    sample_text = """
    # Research Paper
    DOI: 10.1016/j.cell.2024.01.001.
    Duplicate DOI: https://doi.org/10.1016/j.cell.2024.01.001
    PubMed ID: 38245678, also PMID: 38245678.
    Indexed in PMC87654321.
    Trial registered under NCT04567890 and ChiCTR2100045678.
    Author ORCID: 0000-0002-1825-0097.
    """
    results = extract_identifiers(sample_text)
    types_and_values = {(item["type"], item["value"]) for item in results}

    assert ("DOI", "10.1016/j.cell.2024.01.001") in types_and_values
    assert ("PMID", "38245678") in types_and_values
    assert ("PMCID", "PMC87654321") in types_and_values
    assert ("NCT", "NCT04567890") in types_and_values
    assert ("ChiCTR", "ChiCTR2100045678") in types_and_values
    assert ("ORCID", "0000-0002-1825-0097") in types_and_values
    assert len(results) == 6


def test_extract_wechat_article_metadata_dom_and_script() -> None:
    from clipdeck.ingestion.metadata import extract_article_metadata

    sample_html = """
    <html>
    <head><title>脑机接口新进展</title></head>
    <body>
        <h1 class="rich_media_title" id="activity-name">哈医大一院植入式脑机接口临床试验</h1>
        <div id="meta_content">
            <span id="profileBt"><a id="js_name">脑机接口星球</a></span>
        </div>
        <div id="js_content">正文内容...</div>
        <script>
            var createTime = '2026-09-08 20:35';
            var ct = "1788870951";
        </script>
    </body>
    </html>
    """
    meta = extract_article_metadata(html=sample_html, url="https://mp.weixin.qq.com/s/sample123")
    assert meta.title == "哈医大一院植入式脑机接口临床试验"
    assert meta.author == "脑机接口星球"
    assert meta.published_at == "2026-09-08 20:35:51"
    assert meta.platform == "微信公众号"


def test_extract_wechat_article_metadata_html_decode() -> None:
    from clipdeck.ingestion.metadata import extract_article_metadata

    sample_html = """
    <html>
    <body>
        <h1 class="rich_media_title">盛京医院临床试验招募</h1>
        <script>
            var nickname = htmlDecode("神外前沿");
            var createTime = '2026-09-08 15:04';
        </script>
    </body>
    </html>
    """
    meta = extract_article_metadata(html=sample_html, url="https://mp.weixin.qq.com/s/sample456")
    assert meta.title == "盛京医院临床试验招募"
    assert meta.author == "神外前沿"
    assert meta.published_at == "2026-09-08 15:04:00"
    assert meta.platform == "微信公众号"


def test_extract_zhihu_metadata() -> None:
    from clipdeck.ingestion.metadata import extract_article_metadata

    sample_html = """
    <html>
    <head>
        <title>革命不是请客吃饭这句话有多深刻？ - 看看地图 的回答 - 知乎</title>
        <meta itemprop="dateCreated" content="2024-09-04T09:25:09.000Z"/>
        <meta itemprop="dateCreated" content="2026-02-27T08:54:29.000Z"/>
    </head>
    <body>
        <div class="AuthorInfo">
            <span class="UserLink AuthorInfo-name">看看地图</span>
        </div>
        <div class="RichContent-inner">正文回答...</div>
    </body>
    </html>
    """
    meta = extract_article_metadata(html=sample_html, url="https://www.zhihu.com/question/666196787/answer/2010759696290693318")
    assert meta.author == "看看地图"
    assert meta.published_at == "2026-02-27 16:54:29"
    assert meta.platform == "知乎"


