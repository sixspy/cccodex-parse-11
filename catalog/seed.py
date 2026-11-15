from catalog.models import ParseJob

SEEDS = [
    (
        "活动页字段抽取",
        "promo_html",
        "https://mock.local/promo",
        "https://mock.local/promo-main",
        "https://mock.local/promo-supp",
    ),
    (
        "报价单表格解析",
        "quote_xlsx",
        "file://drops/quote-main.xlsx",
        "file://drops/quote-main.xlsx",
        "file://drops/quote-supp.xlsx",
    ),
    (
        "公告正文抽取",
        "notice_html",
        "https://mock.local/notice",
        "https://mock.local/notice-main",
        "https://mock.local/notice-supp",
    ),
    (
        "目录索引解析",
        "index_json",
        "https://mock.local/index.json",
        "https://mock.local/index-main.json",
        "https://mock.local/index-supp.json",
    ),
]


def ensure_seed():
    if ParseJob.objects.count():
        return
    for title, code, uri, primary_uri, secondary_uri in SEEDS:
        ParseJob.objects.create(
            title=title,
            code=code,
            status="待解析",
            source_uri=uri,
            primary_uri=primary_uri,
            secondary_uri=secondary_uri,
        )
