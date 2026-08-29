from sci_radar.acquisition.domain import ResourceClassifier, ResourceType, SourceKind


def test_classifier_routes_wechat_and_regular_web() -> None:
    classifier = ResourceClassifier()
    assert classifier.classify_url("https://mp.weixin.qq.com/s/abc") is ResourceType.WECHAT_ARTICLE
    assert classifier.classify_url("https://example.com/news") is ResourceType.WEB_PAGE


def test_classifier_reserves_media_and_document_routes() -> None:
    classifier = ResourceClassifier()
    assert classifier.classify_url("https://cdn.example.org/talk.mp3") is ResourceType.PODCAST
    assert classifier.classify_url("https://cdn.example.org/demo.mp4") is ResourceType.VIDEO
    assert classifier.classify_url("https://example.org/paper.pdf") is ResourceType.PDF
    assert classifier.classify_upload("report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document") is ResourceType.WORD
    assert classifier.classify_source(SourceKind.TEXT) is ResourceType.TEXT


def test_url_normalization_is_conservative() -> None:
    classifier = ResourceClassifier()
    normalized = classifier.normalize_url("  HTTPS://Example.COM:443/a?q=1#section  ")
    assert normalized == "https://example.com/a?q=1"
