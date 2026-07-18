from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from .models import BetCandidate, LegCandidate, ParseOutcome, RawSource


PARSER_VERSION = "heuristic-v2"

PICK_MARKER_RE = re.compile(
    r"\b(?:pick|bet|play|prediction|selection|tip)\s*[:=\-]\s*(.+)", re.I
)
EVENT_MARKER_RE = re.compile(r"\b(?:event|game|match)\s*[:=\-]\s*(.+)", re.I)
PARLAY_RE = re.compile(r"\b(?:parlay|acca|accumulator|multi|sgp|bet\s*builder|same game)\b", re.I)
CURRENT_RE = re.compile(
    r"\b(?:current(?:\s+pick)?|today(?:'s)?\s+(?:pick|bet)|tonight(?:'s)?\s+(?:pick|bet)|potd\s*#?\s*\d+)\b",
    re.I,
)
PAST_SECTION_RE = re.compile(
    r"\b(?:recent picks?|previous picks?|past picks?|last picks?|record|recap|results?|winning streak|roi|profit)\b",
    re.I,
)
RESULT_ONLY_RE = re.compile(
    r"\b(?:cashed|cash(?:ed)? out|winner|won|hit|swept|craziest hit|last night|yesterday)\b|[✅❌🟢🔴]",
    re.I,
)
MARKET_WORD_RE = re.compile(
    r"(?:\b(?:over|under|btts|both teams|moneyline|ml|to win|draw|double chance|dnb|draw no bet|spread|handicap|goals?|points?|corners?|cards?|shots?|rebounds?|assists?|sets?|touchdowns?|runs?)\b"
    r"|(?<![A-Za-z])(?:o|u)\s*\d{1,3}(?:\.\d{1,2})?\b)",
    re.I,
)
TOTAL_SELECTION_RE = re.compile(
    r"(?:\b(?P<long>over|under)\b|(?<![A-Za-z])(?P<short>[ou]))\s*"
    r"(?P<line>\d{1,3}(?:\.\d{1,2})?)\b",
    re.I,
)

DECIMAL_CONTEXT_PATTERNS = (
    re.compile(r"\b(?:total\s+)?odds?|\bprice", re.I),
    re.compile(r"@"),
)
ODDS_LABELED_RE = re.compile(
    r"\b(?P<label>(?:total\s+)?odds?|price)\s*(?:of\s*)?(?:[:=@\-]\s*)?"
    r"(?P<odds>[+]\d{3,4}|-\d{3,4}|\d{1,3}/\d{1,3}|\d{1,3}(?:\.\d{1,3})?)",
    re.I,
)
ODDS_AT_RE = re.compile(
    r"@\s*(?P<odds>[+]\d{3,4}|-\d{3,4}|\d{1,3}/\d{1,3}|\d{1,3}(?:\.\d{1,3})?)"
)
AMERICAN_RE = re.compile(r"(?<![\d.])(?P<odds>[+-]\d{3,4})(?![\d.])")
FRACTIONAL_RE = re.compile(r"(?<![\d.])(?P<odds>\d{1,3}/\d{1,3})(?![\d.])")

EVENT_PAIR_RE = re.compile(
    r"(?P<home>[A-Za-z0-9][A-Za-z0-9 .&'’()\-/]{1,38}?)\s+"
    r"(?:vs?\.?|versus|@)\s+"
    r"(?P<away>[A-Za-z0-9][A-Za-z0-9 .&'’()\-/]{1,38})",
    re.I,
)
HYPHEN_EVENT_RE = re.compile(
    r"^(?P<home>[A-Za-z][A-Za-z0-9 .&'’()\-/]{1,35}?)\s+-\s+"
    r"(?P<away>[A-Za-z][A-Za-z0-9 .&'’()\-/]{1,35}?)$",
    re.I,
)


@dataclass(slots=True)
class OddsHit:
    decimal: float
    raw: str
    start: int
    end: int
    is_total: bool = False


@dataclass(slots=True)
class LegDraft:
    raw: str
    selection: str
    event_text: str | None
    home: str | None
    away: str | None
    odds: OddsHit | None
    explicit_pick: bool


def decimal_odds(raw: str) -> float | None:
    value = raw.strip().replace("−", "-")
    try:
        if "/" in value:
            numerator, denominator = value.split("/", 1)
            denominator_value = float(denominator)
            if denominator_value == 0:
                return None
            result = 1.0 + float(numerator) / denominator_value
        elif value.startswith("+"):
            result = 1.0 + float(value[1:]) / 100.0
        elif value.startswith("-"):
            american = abs(float(value))
            if american < 100:
                return None
            result = 1.0 + 100.0 / american
        else:
            result = float(value)
    except (ValueError, ZeroDivisionError):
        return None
    if 1.01 <= result <= 1001:
        return round(result, 6)
    return None


def extract_odds(text: str) -> list[OddsHit]:
    hits: list[OddsHit] = []
    occupied: list[tuple[int, int]] = []

    for pattern in (ODDS_LABELED_RE, ODDS_AT_RE):
        for match in pattern.finditer(text):
            value = decimal_odds(match.group("odds"))
            if value is None:
                continue
            span = match.span()
            if any(span[0] < end and span[1] > start for start, end in occupied):
                continue
            occupied.append(span)
            label = match.groupdict().get("label") or ""
            hits.append(
                OddsHit(
                    decimal=value,
                    raw=match.group("odds"),
                    start=span[0],
                    end=span[1],
                    is_total="total" in label.lower(),
                )
            )

    # American and fractional prices are usually explicit enough even without a label.
    for pattern in (AMERICAN_RE, FRACTIONAL_RE):
        for match in pattern.finditer(text):
            span = match.span()
            if any(span[0] < end and span[1] > start for start, end in occupied):
                continue
            value = decimal_odds(match.group("odds"))
            if value is None:
                continue
            occupied.append(span)
            hits.append(OddsHit(value, match.group("odds"), span[0], span[1]))
    return sorted(hits, key=lambda item: item.start)


def _clean_markdown(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _focus_current_section(text: str) -> str:
    past_match = PAST_SECTION_RE.search(text)
    current_matches = list(CURRENT_RE.finditer(text))
    if past_match and current_matches:
        later = [match for match in current_matches if match.start() > past_match.start()]
        if later:
            return text[later[-1].start() :]
    return text


def _strip_line(line: str) -> str:
    value = line.strip()
    value = re.sub(r"^[>\-*•]+\s*", "", value)
    value = re.sub(r"^[⭐🔥]+\s*", "", value)
    return value.strip()


def _is_past_line(line: str, in_current_builder: bool) -> bool:
    stripped = _strip_line(line)
    if not stripped:
        return False
    if stripped.startswith(("❌", "🟥", "🔴")):
        return True
    if stripped.startswith(("✅", "🟩", "🟢")) and not in_current_builder:
        return True
    if PAST_SECTION_RE.match(stripped) and not CURRENT_RE.search(stripped):
        return True
    if re.match(r"^(?:W|L|P){2,}\b", stripped, re.I):
        return True
    return False


def _clean_team(value: str) -> str:
    value = re.sub(r"^(?:event|game|match)\s*[:=\-]\s*", "", value, flags=re.I)
    value = re.sub(r"^(?:current\s+)?potd\s*#?\s*\d*\s*", "", value, flags=re.I)
    value = re.sub(r"\s+(?:pick|bet|prediction|odds?)\s*[:=\-].*$", "", value, flags=re.I)
    value = re.sub(r"\s+(?:bet\s*builder|parlay|acca|multi)\s*$", "", value, flags=re.I)
    value = re.sub(r"\b(?:ml|moneyline|to win)\b.*$", "", value, flags=re.I)
    value = re.sub(r"\s+[+-]\d{3,4}\s*$", "", value)
    value = re.sub(r"\s{2,}", " ", value)
    return value.strip(" -–—:|.,")


def extract_event_pair(text: str) -> tuple[str, str, str] | None:
    for raw_line in text.splitlines() or [text]:
        line = _strip_line(raw_line)
        line = re.sub(r"^(?:event|game|match)\s*[:=\-]\s*", "", line, flags=re.I)
        line = re.sub(r"^(?:current\s+)?potd\s*#?\s*\d*\s*", "", line, flags=re.I)
        event_part = line
        for separator in (" - ", " – ", " — ", " | ", " : "):
            if separator not in line:
                continue
            left, right = line.split(separator, 1)
            if EVENT_PAIR_RE.search(left) and MARKET_WORD_RE.search(right):
                event_part = left
                break
        compact_event_part = re.split(r"\s*[:|]\s*", event_part, maxsplit=1)[0]
        match = EVENT_PAIR_RE.search(event_part) or HYPHEN_EVENT_RE.search(compact_event_part)
        if not match:
            continue
        home = _clean_team(match.group("home"))
        away = _clean_team(match.group("away"))
        if not home or not away:
            continue
        return home, away, f"{home} vs {away}"
    return None


def infer_sport(subreddit: str, title: str, flair: str | None, body: str) -> tuple[str | None, str | None]:
    context = " ".join((subreddit, title, flair or "", body[:500])).lower()
    if subreddit.lower() == "soccerbetting" or re.search(r"\bsoccer\b", context):
        league = "World Cup" if "world cup" in context else None
        return "Soccer", league
    mappings = (
        (r"\b(?:nfl|ncaaf|american football)\b", "American Football", None),
        (r"\b(?:nba|wnba|ncaab|basketball)\b", "Basketball", None),
        (r"\b(?:mlb|baseball)\b", "Baseball", None),
        (r"\b(?:nhl|ice hockey|hockey)\b", "Ice Hockey", None),
        (r"\b(?:atp|wta|tennis)\b", "Tennis", None),
        (r"\b(?:ufc|mma|mixed martial)\b", "Mixed Martial Arts", None),
        (r"\b(?:cricket|ipl)\b", "Cricket", None),
        (r"\b(?:golf|pga|lpga)\b", "Golf", None),
        (r"\b(?:esports|counter-strike|cs2|league of legends|dota)\b", "Esports", None),
        (r"\b(?:rugby)\b", "Rugby", None),
    )
    for pattern, sport, league in mappings:
        if re.search(pattern, context):
            return sport, league
    if re.search(r"\b(?:epl|premier league|la liga|bundesliga|serie a|uefa|fifa|football)\b", context):
        return "Soccer", "World Cup" if "world cup" in context else None
    return None, None


def classify_market(selection: str) -> tuple[str, str | None, float | None]:
    lower = selection.lower()
    number_match = TOTAL_SELECTION_RE.search(lower)
    line_value = float(number_match.group("line")) if number_match else None

    if "btts" in lower or "both teams to score" in lower:
        side = "No" if re.search(r"\bno\b", lower) else "Yes"
        return "btts", side, None
    if "double chance" in lower or re.search(r"\b(?:1x|x2|12)\b", lower):
        return "double_chance", selection.strip(), None
    if "draw no bet" in lower or re.search(r"\bdnb\b", lower):
        side = re.sub(r"\b(?:draw no bet|dnb)\b", "", selection, flags=re.I).strip(" -:@")
        return "draw_no_bet", side or None, None
    if number_match:
        direction = (number_match.group("long") or number_match.group("short") or "").lower()
        side = "Over" if direction.startswith("o") else "Under"
        if "corner" in lower:
            return "corners_total", side, line_value
        if "card" in lower or "booking" in lower:
            return "cards_total", side, line_value
        if any(word in lower for word in ("shot", "assist", "rebound", "touchdown", "player")):
            return "player_prop", side, line_value
        return "total", side, line_value
    if "spread" in lower or "handicap" in lower or re.search(r"\b[+-]\d+(?:\.\d+)?\b", lower):
        line_match = re.search(r"(?<!\d)([+-]\d+(?:\.\d+)?)", lower)
        return "spread", selection.strip(), float(line_match.group(1)) if line_match else None
    if re.search(r"\b(?:ml|moneyline|to win|win)\b", lower):
        side = re.sub(r"\b(?:ml|moneyline|to win|win)\b", "", selection, flags=re.I)
        return "h2h", side.strip(" -:@") or selection.strip(), None
    if lower.strip() == "draw" or re.search(r"\bdraw\b", lower):
        return "h2h", "Draw", None
    return "custom", selection.strip(), None


def _remove_odds_fragment(text: str) -> str:
    value = ODDS_LABELED_RE.sub("", text)
    value = ODDS_AT_RE.sub("", value)
    value = re.sub(r"\b(?:stake|units?)\s*[:=\-]?\s*\d+(?:\.\d+)?\s*u?\b.*$", "", value, flags=re.I)
    return value.strip(" -–—:|,;")


def _split_builder_selection(selection: str) -> list[str]:
    parts = re.split(r"\s+(?:\+|&|and)\s+", selection, flags=re.I)
    cleaned = [part.strip(" -–—:|,;") for part in parts if MARKET_WORD_RE.search(part)]
    return cleaned if len(cleaned) > 1 else [selection.strip()]


def _line_odds(line: str) -> OddsHit | None:
    hits = extract_odds(line)
    if hits:
        return hits[-1]
    if not MARKET_WORD_RE.search(line):
        return None
    decimals = list(re.finditer(r"(?<![\d.])(\d{1,2}\.\d{1,3})(?![\d.])", line))
    if len(decimals) >= 2:
        match = decimals[-1]
    elif len(decimals) == 1 and re.search(
        r"\b(?:ml|moneyline|to win|draw|btts|both teams|parlay|acca)\b", line, re.I
    ):
        match = decimals[0]
    else:
        return None
    value = decimal_odds(match.group(1))
    return OddsHit(value, match.group(1), match.start(), match.end()) if value else None


def _drafts(text: str) -> list[LegDraft]:
    drafts: list[LegDraft] = []
    current_event: tuple[str, str, str] | None = None
    current_builder = False
    lines = text.splitlines()
    if len(lines) == 1:
        # Common Reddit markdown uses sentences instead of line breaks.
        expanded = re.sub(
            r"\s+(?=(?:Event|Game|Match|Pick|Bet|Play|Prediction|Selection|Odds|Total Odds)\s*[:=\-])",
            "\n",
            text,
            flags=re.I,
        )
        if PARLAY_RE.search(expanded):
            expanded = re.sub(r"\s+(?=[✅☑✔])", "\n", expanded)
        lines = expanded.splitlines()

    for raw_line in lines:
        line = _strip_line(raw_line)
        if not line:
            continue
        if PARLAY_RE.search(line):
            current_builder = True
        if _is_past_line(line, current_builder):
            continue

        event = extract_event_pair(line)
        if event:
            current_event = event

        marker = PICK_MARKER_RE.search(line)
        explicit = marker is not None
        selection: str | None = marker.group(1).strip() if marker else None

        if selection is None and event:
            for separator in (" - ", " – ", " — ", " | ", " : "):
                if separator not in line:
                    continue
                _, right = line.split(separator, 1)
                if MARKET_WORD_RE.search(right):
                    selection = right.strip()
                    break

        if selection is None and current_builder and line.startswith(("✅", "☑", "✔")):
            selection = line.lstrip("✅☑✔ ").strip()
        if selection is None and MARKET_WORD_RE.search(line) and _line_odds(line):
            # A compact form such as "France ML @ 1.85".
            selection = line
        if selection is None and current_builder and MARKET_WORD_RE.search(line):
            if not re.match(r"^(?:odds?|stake|write[- ]?up|analysis)\b", line, re.I):
                selection = line
        if selection is None:
            continue

        selection = _remove_odds_fragment(selection)
        if event and marker:
            # If the event and pick share a line, retain only the text after Pick:.
            selection = _remove_odds_fragment(marker.group(1))
        if not selection or not MARKET_WORD_RE.search(selection):
            continue

        pieces = _split_builder_selection(selection) if current_builder else [selection]
        for piece in pieces:
            drafts.append(
                LegDraft(
                    raw=line,
                    selection=piece,
                    event_text=current_event[2] if current_event else None,
                    home=current_event[0] if current_event else None,
                    away=current_event[1] if current_event else None,
                    odds=_line_odds(line),
                    explicit_pick=explicit,
                )
            )
    return _deduplicate_drafts(drafts)


def _deduplicate_drafts(drafts: Iterable[LegDraft]) -> list[LegDraft]:
    result: list[LegDraft] = []
    seen: set[tuple[str, str | None]] = set()
    for draft in drafts:
        key = (re.sub(r"\W+", "", draft.selection.lower()), draft.event_text)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        result.append(draft)
    return result


def _to_leg(draft: LegDraft, sport: str | None, league: str | None) -> LegCandidate:
    market_type, side, line_value = classify_market(draft.selection)
    return LegCandidate(
        raw_text=draft.raw,
        selection=draft.selection,
        market_type=market_type,
        side=side,
        line_value=line_value,
        event_text=draft.event_text,
        home_team_hint=draft.home,
        away_team_hint=draft.away,
        sport=sport,
        league=league,
        quoted_odds_decimal=draft.odds.decimal if draft.odds else None,
    )


def parse_source(source: RawSource) -> ParseOutcome:
    text = _focus_current_section(_clean_markdown(source.body))
    if len(text.strip()) < 4:
        return ParseOutcome(rejection_reason="empty_or_image_only")
    has_pick_signal = bool(PICK_MARKER_RE.search(text) or MARKET_WORD_RE.search(text))
    if RESULT_ONLY_RE.search(text) and not has_pick_signal:
        return ParseOutcome(rejection_reason="past_result_or_win_post")

    sport, league = infer_sport(
        source.subreddit, source.parent_title, source.flair, text
    )
    all_odds = extract_odds(text)
    drafts = _drafts(text)
    if not drafts:
        return ParseOutcome(rejection_reason="no_explicit_pick_found")

    is_parlay = bool(PARLAY_RE.search(text)) or len(drafts) > 1 and any(
        hit.is_total for hit in all_odds
    )
    total_odds = next((hit for hit in reversed(all_odds) if hit.is_total), None)

    if len(drafts) > 1 and not is_parlay and all(draft.odds for draft in drafts):
        candidates: list[BetCandidate] = []
        for draft in drafts:
            leg = _to_leg(draft, sport, league)
            confidence = _confidence([draft], draft.odds, sport, False)
            candidates.append(
                BetCandidate(
                    bet_type="SINGLE",
                    description=_description([leg]),
                    quoted_odds_decimal=draft.odds.decimal if draft.odds else None,
                    original_odds_text=draft.odds.raw if draft.odds else None,
                    sport=sport,
                    league=league,
                    legs=[leg],
                    parser_confidence=confidence,
                    parser_notes=["Multiple independently priced picks found in one source."],
                )
            )
        return ParseOutcome(candidates=candidates)

    legs = [_to_leg(draft, sport, league) for draft in drafts]
    chosen_odds = total_odds
    if chosen_odds is None:
        if len(drafts) == 1 and drafts[0].odds:
            chosen_odds = drafts[0].odds
        elif all_odds:
            chosen_odds = all_odds[-1]

    bet_type = "PARLAY" if is_parlay or len(legs) > 1 else "SINGLE"
    notes: list[str] = []
    if bet_type == "PARLAY":
        # A combined price printed once beside several same-line legs must never be
        # mistaken for an individual leg price. Keeping this distinction lets push
        # adjustments be automatic only when genuine per-leg prices were posted.
        signatures = [
            (draft.raw, draft.odds.start, draft.odds.end, draft.odds.raw)
            for draft in drafts
            if draft.odds
        ]
        shared = {signature for signature, count in Counter(signatures).items() if count > 1}
        cleared_combined_price = False
        for leg, draft in zip(legs, drafts):
            if not draft.odds:
                continue
            signature = (draft.raw, draft.odds.start, draft.odds.end, draft.odds.raw)
            if draft.odds.is_total or signature in shared:
                leg.quoted_odds_decimal = None
                cleared_combined_price = True
        if cleared_combined_price:
            notes.append("Combined parlay price was kept separate from individual leg prices.")
    if chosen_odds is None:
        notes.append("No usable quoted odds found; manual review required.")
    if any(leg.market_type == "custom" for leg in legs):
        notes.append("At least one market could not be classified automatically.")
    if bet_type == "PARLAY" and not PARLAY_RE.search(text):
        notes.append("Multiple legs inferred from a shared total price.")
    confidence = _confidence(drafts, chosen_odds, sport, bet_type == "PARLAY")
    return ParseOutcome(
        candidates=[
            BetCandidate(
                bet_type=bet_type,
                description=_description(legs),
                quoted_odds_decimal=chosen_odds.decimal if chosen_odds else None,
                original_odds_text=chosen_odds.raw if chosen_odds else None,
                sport=sport,
                league=league,
                legs=legs,
                parser_confidence=confidence,
                parser_notes=notes,
            )
        ]
    )


def _description(legs: list[LegCandidate]) -> str:
    parts = []
    for leg in legs:
        if leg.event_text:
            parts.append(f"{leg.event_text}: {leg.selection}")
        else:
            parts.append(leg.selection)
    return " + ".join(parts)


def _confidence(
    drafts: list[LegDraft], odds: OddsHit | None, sport: str | None, parlay: bool
) -> float:
    score = 0.25
    if odds:
        score += 0.22
    if all(draft.explicit_pick for draft in drafts):
        score += 0.16
    elif any(draft.explicit_pick for draft in drafts):
        score += 0.08
    if all(draft.event_text for draft in drafts):
        score += 0.18
    elif any(draft.event_text for draft in drafts):
        score += 0.08
    if sport:
        score += 0.08
    if parlay and len(drafts) >= 2:
        score += 0.05
    if any(classify_market(draft.selection)[0] == "custom" for draft in drafts):
        score -= 0.12
    return round(max(0.0, min(0.98, score)), 3)
