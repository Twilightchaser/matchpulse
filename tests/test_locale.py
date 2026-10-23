from pathlib import Path
from app.agents import InsightWorkflow
from app.metrics import MetricsEngine
from app.language import chinese_copy_problem


def test_chinese_model_copy_rejects_english_terms_but_preserves_names_and_units():
    names = ["Northbridge FC", "N. Player 10"]
    assert chinese_copy_problem("N. Player 10 为 Northbridge FC 破门，0.27 xG，87.4 km/h [evt-0581]。", names) is None
    assert chinese_copy_problem("Northbridge FC momentum share（势头占比）69.3% vs 30.7%。", names)
    assert chinese_copy_problem("Northbridge FC scored.", names)


def test_chinese_recap_fallback_and_language_isolation(events,match):
    snap=MetricsEngine().snapshot(events,90,home_team=match.home_team,away_team=match.away_team)
    wf=InsightWorkflow()
    zh=wf.recap(events,snap,"zh")
    en=wf.recap(events,snap,"en")
    assert "进球" in zh["summary"] and zh["language"]=="zh"
    assert en["language"]=="en" and zh["summary"]!=en["summary"]
    assert wf.recap_agent.validate(zh["summary"],zh,snap)[0]


def test_utf8_assets_are_clean_and_reference_fonts_present():
    for path in Path("app/static").glob("*"):
        if path.suffix in {".js",".css",".html"}:
            text=path.read_bytes().decode("utf-8",errors="strict")
            assert "\ufffd" not in text and "锟斤拷" not in text
    assert Path("app/static/fonts/barlow-condensed-latin-700-normal.woff2").exists()
