"""House style (docs/WRITING_STYLE.md): what viewers read must sound like a broadcast, not a report.

These run on the live match the studio serves, in all four audience profiles.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.agents import EvidenceGuardianAgent, InsightWorkflow, MomentExplainerAgent, SharedMatchState, STATE_KINDS
from app.house_style import banned_phrase, chance_words, minute_mark, reason_zh, short_club, surname
from app.language import chinese_copy_problem
from app.main import app, build_overlay_timeline, build_snapshot, match_events

client = TestClient(app)
PROFILES = [("fan", "en"), ("analyst", "en"), ("fan", "zh"), ("analyst", "zh")]


@pytest.fixture(scope="module")
def timelines():
    return {profile: build_overlay_timeline(*profile, None, None)["cards"] for profile in PROFILES}


def test_helpers():
    assert minute_mark(57) == 58 and minute_mark(0) == 1 and minute_mark(90) == 90
    assert chance_words(0.51) == "a big chance" and chance_words(0.07, "zh") == "半个机会"
    assert short_club("Northbridge FC") == "Northbridge" and short_club("Riverside Athletic") == "Riverside"
    assert surname("Elias Nouri") == "Nouri" and surname("N. Player 10") == "N. Player 10"
    assert surname("Sam Hart", {"Sam Hart", "Leo Hart"}) == "Sam Hart"  # ambiguous surname stays whole
    assert banned_phrase("It is worth noting that Riverside pressed.") == "it is worth noting"
    assert banned_phrase("这一进球彰显了实力") == "彰显"
    assert banned_phrase("Riverside pressed high.") is None
    assert reason_zh("numbers not backed by evidence: 0.87, 64") == "这些数字在证据里找不到：0.87, 64"
    assert reason_zh("banned phrase: 彰显; empty narrative") == "用了写作规范禁止的说法：彰显；文案为空"


def test_guardian_rejects_report_phrasing():
    snapshot = build_snapshot(57, 17)
    workflow = InsightWorkflow(stateful=False)
    candidate = workflow.analyst.candidates(snapshot)[0]
    candidate.explanation = MomentExplainerAgent.explain(candidate) + " This highlights Riverside's resilience."
    ok, reason = EvidenceGuardianAgent().run(candidate, SharedMatchState(snapshot=snapshot))
    assert not ok and reason == "banned phrase: this highlights"


def test_every_card_reads_clean(timelines):
    names = [e.player_name for e in match_events()] + ["Northbridge FC", "Riverside Athletic"]
    for (mode, language), cards in timelines.items():
        assert cards, (mode, language)
        for card in cards:
            text = f"{card['headline']} {card['body']}"
            assert banned_phrase(text) is None, text
            assert "evidence" not in text.lower() and "·" not in text, text
            if language == "zh":
                assert chinese_copy_problem(text, names) is None, text
                assert not re.search(r"[A-Za-z]{3,}", card["eyebrow"]), card["eyebrow"]
            if mode == "fan":
                assert not re.search(r"\d\.\d", text), text  # fans get words, not decimals


def test_live_replay_does_not_repeat_itself(timelines):
    for cards in timelines.values():
        bodies = [card["body"] for card in cards]
        repeats = [b for i, b in enumerate(bodies) if b in bodies[:i]]
        assert not repeats, repeats
        for previous, current in zip(cards, cards[1:]):
            assert previous["headline"] != current["headline"]


def test_no_spell_cards_before_a_spell_exists(timelines):
    for cards in timelines.values():
        assert all(c["display_at_second"] >= 8 * 60 for c in cards if c["kind"] in STATE_KINDS)
    # The old 04:00 card claimed "100% vs 0%" momentum from two progressive passes.
    assert client.get("/api/insight", params={"minute": 4, "second": 0}).json()["overlay"] is None


def test_traces_are_written_for_both_languages():
    body = client.get("/api/insight", params={"minute": 57, "second": 17, "language": "zh"}).json()
    assert body["trace"] and all(t["summary_zh"] for t in body["trace"])
    joined = " ".join(t["summary"] for t in body["trace"])
    assert "evidence-grounded" not in joined and "处理结果可通过" not in " ".join(t["summary_zh"] for t in body["trace"])


def test_state_card_is_timed_to_the_completed_window():
    from app.agents import AudienceDirectorAgent, AudienceProfile
    from app.models import Evidence, InsightCandidate

    snapshot = build_snapshot(8, 0)
    earlier = max((e for e in snapshot.event_index.values() if e.match_second < 480),
                  key=lambda e: e.sequence)
    candidate = InsightCandidate(
        insight_id="window-at-eight-minutes", team_id=snapshot.home_team, minute=8,
        kind="rhythm", importance=.78, headline="The game opens up", explanation="Possession changes more often.",
        trigger_event_id=earlier.event_id,
        evidence=[Evidence(label="Window event", value=earlier.clock, event_ids=[earlier.event_id])],
    )
    card, _ = AudienceDirectorAgent().run(candidate, True, AudienceProfile(mode="analyst"), snapshot)
    assert card.display_at_second == 480
    assert card.evidence[0].event_ids == [earlier.event_id]


def test_recap_tells_the_story_in_both_languages():
    full = build_snapshot(90)
    workflow = InsightWorkflow()
    en = workflow.recap(list(match_events()), full, "en")
    zh = workflow.recap(list(match_events()), full, "zh")
    assert en["summary"].startswith("Riverside Athletic came from behind to win 2-1")
    assert "18'" in en["summary"] and "58'" in en["summary"] and "17:46" not in en["summary"]
    assert "逆转" in zh["summary"] and "第58分钟" in zh["summary"]
    for result in (en, zh):
        assert workflow.recap_agent.validate(result["summary"], result, full)[0]
        assert banned_phrase(result["summary"]) is None
        # Only the final score may appear as a scoreline; earlier scores are told in words.
        assert set(re.findall(r"\b\d-\d\b", result["summary"])) == {"2-1"}


def test_commentary_is_curated_and_bilingual():
    en = client.get("/api/commentary", params={"language": "en", "mode": "analyst"}).json()
    zh = client.get("/api/commentary", params={"language": "zh"}).json()
    assert 40 <= len(en) <= 120 and len(en) == len(zh)
    assert [line["second"] for line in en] == sorted(line["second"] for line in en)
    goals = [e for e in match_events() if e.outcome == "goal"]
    assert [l["event_id"] for l in en if l["kind"] == "goal"] == [g.event_id for g in goals]
    assert en[0]["kind"] == "kickoff" and en[-1]["kind"] == "full_time"
    assert any(l["kind"] == "half_time" for l in en)
    assert all(line["detail"] for line in en if line["kind"] in {"goal", "shot", "big_chance"})
    assert not any(line["detail"] for line in zh)  # fan mode: no data line
    names = [e.player_name for e in match_events()] + ["Northbridge FC", "Riverside Athletic"]
    assert all(chinese_copy_problem(line["text"], names) is None for line in zh)
    assert all(banned_phrase(line["text"]) is None for line in en + zh)


def test_highlights_are_the_story_not_raw_events():
    moments = client.get("/api/highlights", params={"language": "en"}).json()
    assert 5 <= len(moments) <= 12
    assert [m["second"] for m in moments] == sorted(m["second"] for m in moments)
    goals = [e for e in match_events() if e.outcome == "goal"]
    assert {m["second"] for m in moments if m["kind"] == "goal"} == {g.match_second for g in goals}
    assert all(m["headline"] and m["minute"] == min(90, m["second"] // 60 + 1) for m in moments)
    zh = client.get("/api/highlights", params={"language": "zh"}).json()
    assert [m["second"] for m in zh] == [m["second"] for m in moments] and zh[0]["label"] != moments[0]["label"]
