#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / 'config.json'
DOMAIN_CATEGORIES_PATH = ROOT / 'domain_categories.json'
UNCLASSIFIED_DOMAINS_PATH = ROOT / 'unclassified_domains.txt'
ACTIVITY_INPUT_DIR = ROOT / 'inputs' / 'activity'
OUTPUT_DAILY = ROOT / 'outputs' / 'daily'
OUTPUT_DATA = ROOT / 'outputs' / 'data'
DOMAIN_CACHE_PATH = OUTPUT_DATA / 'domain_categories_cache.json'


@dataclass(frozen=True)
class Visit:
    url: str
    normalized_url: str
    title: str
    normalized_title: str
    domain: str
    category: str
    visited_at: str
    source: str


@dataclass(frozen=True)
class TimedVisit:
    visit: Visit
    estimated_seconds: int


def load_json(path: Path) -> dict:
    with path.open('r', encoding='utf-8') as f:
        return json.load(f)


def load_domain_categories() -> dict[str, str]:
    """Load domain→category mappings from domain_categories.json."""
    if DOMAIN_CATEGORIES_PATH.exists():
        try:
            return json.loads(DOMAIN_CATEGORIES_PATH.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def save_domain_categories(mapping: dict[str, str]) -> None:
    DOMAIN_CATEGORIES_PATH.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True),
        encoding='utf-8',
    )


def ensure_dirs() -> None:
    OUTPUT_DAILY.mkdir(parents=True, exist_ok=True)
    OUTPUT_DATA.mkdir(parents=True, exist_ok=True)
    ACTIVITY_INPUT_DIR.mkdir(parents=True, exist_ok=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument('--day', default=None, help='today | yesterday | YYYY-MM-DD')
    p.add_argument('--all-days', action='store_true', help='Analyze all days found in input files')
    return p.parse_args()


def resolve_day(day_arg: str | None, tz_name: str) -> datetime.date:
    now = datetime.now().astimezone()
    if not day_arg or day_arg == 'yesterday':
        return (now - timedelta(days=1)).date()
    if day_arg == 'today':
        return now.date()
    return datetime.strptime(day_arg, '%Y-%m-%d').date()


def normalize_title(title: str) -> str:
    return ' '.join((title or '').strip().lower().split())


def strip_tracking_params(query_pairs: list[tuple[str, str]], config: dict) -> list[tuple[str, str]]:
    prefixes = tuple(config.get('strip_query_params_prefixes', []))
    exact = set(config.get('strip_query_params_exact', []))
    kept = []
    for k, v in query_pairs:
        lk = k.lower()
        if lk in exact:
            continue
        if prefixes and any(lk.startswith(prefix) for prefix in prefixes):
            continue
        kept.append((k, v))
    return kept


def normalize_url(url: str, config: dict) -> str:
    try:
        parts = urlsplit(url)
    except Exception:
        return url.strip()
    scheme = (parts.scheme or '').lower()
    netloc = (parts.netloc or '').lower()
    path = parts.path or ''
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    query_pairs = strip_tracking_params(query_pairs, config)
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit((scheme, netloc, path.rstrip('/') or '/', query, ''))


def domain_of(url: str) -> str:
    try:
        return (urlsplit(url).netloc or '').lower()
    except Exception:
        return ''


def _keyword_matches(kw: str, text: str) -> bool:
    """Match keyword with word boundaries to avoid partial matches.

    'api' won't match 'rapidapi', 'mail' won't match 'facebookmail', etc.
    Boundaries are defined as: not preceded/followed by alphanumeric chars.
    """
    pattern = r'(?<![a-zA-Z0-9])' + re.escape(kw) + r'(?![a-zA-Z0-9])'
    return bool(re.search(pattern, text, re.IGNORECASE))


def category_for(url: str, title: str, config: dict) -> str:
    host = domain_of(url)
    # Check domain_categories.json first (highest priority)
    domain_categories: dict[str, str] = config.get('_domain_categories', {})
    if domain_categories and host:
        if host in domain_categories:
            return domain_categories[host]
        # Also check parent domains (sub.example.com → example.com)
        parts = host.split('.')
        for i in range(1, len(parts) - 1):
            parent = '.'.join(parts[i:])
            if parent in domain_categories:
                return domain_categories[parent]

    text = f"{url} {title}".lower()
    categories = config.get('categories', {})
    scores: dict[str, int] = defaultdict(int)
    for cat, rules in categories.items():
        for d in rules.get('domains', []):
            d = d.lower()
            if host == d or host.endswith('.' + d) or d in host:
                scores[cat] += 3
        for kw in rules.get('keywords', []):
            if _keyword_matches(kw.lower(), text):
                scores[cat] += 1
    if not scores:
        return '其他'
    best = sorted(scores.items(), key=lambda x: (-x[1], x[0]))[0]
    return best[0] if best[1] > 0 else '其他'


def visit_dedupe_key(v: Visit) -> tuple[str, str, str]:
    minute_bucket = v.visited_at[:16]
    return (v.normalized_url, minute_bucket, v.normalized_title)


def validate_iso_datetime(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False


def read_activity_jsonl(path: Path, source_name: str, config: dict) -> list[Visit]:
    visits: list[Visit] = []
    skipped = 0
    with path.open('r', encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                print(f'[analyze] {path.name}:{lineno}: JSON parse error: {e}', file=sys.stderr)
                skipped += 1
                continue
            url = obj.get('url', '').strip()
            title = obj.get('title', '') or ''
            visited_at = obj.get('visited_at', '').strip()
            if not url:
                skipped += 1
                continue
            if not visited_at or not validate_iso_datetime(visited_at):
                print(f'[analyze] {path.name}:{lineno}: invalid or missing visited_at={visited_at!r}, skipping', file=sys.stderr)
                skipped += 1
                continue
            nurl = normalize_url(url, config)
            source = obj.get('source') or source_name
            visits.append(Visit(
                url=url,
                normalized_url=nurl,
                title=title,
                normalized_title=normalize_title(title),
                domain=domain_of(nurl),
                category=category_for(nurl, title, config),
                visited_at=visited_at,
                source=source,
            ))
    if skipped:
        print(f'[analyze] {path.name}: skipped {skipped} invalid record(s)', file=sys.stderr)
    return visits


def load_all_visits(config: dict) -> list[Visit]:
    visits: list[Visit] = []
    for path in sorted(ACTIVITY_INPUT_DIR.glob('*.jsonl')):
        visits.extend(read_activity_jsonl(path, path.stem, config))
    return visits


def dedupe_visits(visits: Iterable[Visit]) -> list[Visit]:
    chosen: dict[tuple[str, str, str], Visit] = {}
    for v in visits:
        k = visit_dedupe_key(v)
        if k not in chosen:
            chosen[k] = v
    return sorted(chosen.values(), key=lambda x: x.visited_at)


def filter_by_day(visits: Iterable[Visit], day) -> list[Visit]:
    result = []
    for v in visits:
        try:
            dt = datetime.fromisoformat(v.visited_at)
        except Exception:
            continue
        if dt.astimezone().date() == day:
            result.append(v)
    return result


def get_all_days(visits: Iterable[Visit]) -> list:
    """Return sorted list of all unique dates present in visits."""
    days = set()
    for v in visits:
        try:
            dt = datetime.fromisoformat(v.visited_at)
            days.add(dt.astimezone().date())
        except Exception:
            pass
    return sorted(days)


# ── Session-based time estimation ────────────────────────────────────────────
def estimate_time_spent(visits: list[Visit], config: dict) -> list[TimedVisit]:
    """Estimate time spent per visit using session detection.

    A new session starts when the gap to the next visit exceeds max_gap_seconds.
    The last visit in each session (and the overall last visit) gets default_gap_seconds.
    This prevents a 2-hour idle gap from inflating the previous page's time.
    """
    if not visits:
        return []
    default_gap = int(config.get('default_gap_seconds', 30))
    max_gap = int(config.get('max_gap_seconds', 1800))
    same_domain_bonus = int(config.get('same_domain_continuation_bonus_seconds', 60))

    timed: list[TimedVisit] = []
    parsed = []
    for v in visits:
        try:
            parsed.append((v, datetime.fromisoformat(v.visited_at)))
        except Exception:
            continue

    for i, (visit, dt) in enumerate(parsed):
        if i + 1 < len(parsed):
            next_visit, next_dt = parsed[i + 1]
            gap = int((next_dt - dt).total_seconds())
            if gap < 0 or gap >= max_gap:
                # End of session: don't use the large gap, assign default
                seconds = default_gap
            else:
                seconds = gap
                # Apply same-domain minimum to keep continuations from being too short
                if visit.domain and visit.domain == next_visit.domain:
                    seconds = max(seconds, same_domain_bonus)
        else:
            seconds = default_gap
        timed.append(TimedVisit(visit=visit, estimated_seconds=max(1, seconds)))
    return timed


# ── Claude-assisted domain categorization ────────────────────────────────────

def load_domain_cache() -> dict[str, str]:
    if DOMAIN_CACHE_PATH.exists():
        try:
            return json.loads(DOMAIN_CACHE_PATH.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def save_domain_cache(cache: dict[str, str]) -> None:
    OUTPUT_DATA.mkdir(parents=True, exist_ok=True)
    DOMAIN_CACHE_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True),
        encoding='utf-8',
    )


def claude_categorize_domains(domains: list[str], category_names: list[str]) -> dict[str, str]:
    """Call Claude API to classify unknown domains into categories."""
    if not _ANTHROPIC_AVAILABLE:
        print('[analyze] anthropic package not installed, skipping Claude categorization', file=sys.stderr)
        return {}
    if not domains:
        return {}

    client = anthropic.Anthropic()
    cat_list = '、'.join(category_names)
    domain_list = '\n'.join(f'- {d}' for d in domains)

    prompt = (
        f'请将以下网站域名分类到最合适的类别中。\n'
        f'可用类别：{cat_list}、其他\n\n'
        f'域名列表：\n{domain_list}\n\n'
        f'只返回 JSON 对象，格式为 {{"域名": "类别"}}，不要包含其他内容。\n'
        f'如果无法判断，使用"其他"。'
    )

    try:
        message = client.messages.create(
            model='claude-opus-4-6',
            max_tokens=1024,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'```\s*$', '', raw, flags=re.MULTILINE)
        result = json.loads(raw.strip())
        # Validate result values are strings
        return {k: str(v) for k, v in result.items() if isinstance(k, str)}
    except Exception as e:
        print(f'[analyze] Claude categorization failed: {e}', file=sys.stderr)
        return {}


def enrich_categories_with_claude(visits: list[Visit], config: dict) -> list[Visit]:
    """Re-categorize '其他' visits using Claude API for unknown domains.

    Results are cached in outputs/data/domain_categories_cache.json.
    """
    category_names = list(config.get('categories', {}).keys())
    cache = load_domain_cache()

    # Find domains still in '其他' that aren't cached yet
    uncached: set[str] = set()
    for v in visits:
        if v.category == '其他' and v.domain and v.domain not in cache:
            # Skip synthetic activity.local URLs
            if not v.domain.startswith('activity.local'):
                uncached.add(v.domain)

    if uncached:
        print(f'[analyze] Asking Claude to classify {len(uncached)} unknown domain(s)...', file=sys.stderr)
        new_cats = claude_categorize_domains(sorted(uncached), category_names)
        cache.update(new_cats)
        save_domain_cache(cache)
        for domain, cat in new_cats.items():
            print(f'[analyze]   {domain} → {cat}', file=sys.stderr)

    # Apply cache overrides: rebuild visits where category changes
    result: list[Visit] = []
    for v in visits:
        override = cache.get(v.domain)
        if override and override != v.category:
            v = Visit(
                url=v.url,
                normalized_url=v.normalized_url,
                title=v.title,
                normalized_title=v.normalized_title,
                domain=v.domain,
                category=override,
                visited_at=v.visited_at,
                source=v.source,
            )
        result.append(v)
    return result


# ── Reporting ─────────────────────────────────────────────────────────────────

def format_duration(seconds: int) -> str:
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f'{hours}h {minutes}m {secs}s'
    if minutes:
        return f'{minutes}m {secs}s'
    return f'{secs}s'


def format_report(day_str: str, visits: list[Visit], config: dict) -> str:
    total = len(visits)
    timed_visits = estimate_time_spent(visits, config)
    total_seconds = sum(tv.estimated_seconds for tv in timed_visits)
    category_counts = Counter(v.category for v in visits)
    category_time = defaultdict(int)
    for tv in timed_visits:
        category_time[tv.visit.category] += tv.estimated_seconds
    domain_counts = Counter(v.domain for v in visits if v.domain)
    domain_time = defaultdict(int)
    for tv in timed_visits:
        if tv.visit.domain:
            domain_time[tv.visit.domain] += tv.estimated_seconds
    url_counts = Counter(v.normalized_url for v in visits if v.normalized_url)
    url_time = defaultdict(int)
    for tv in timed_visits:
        if tv.visit.normalized_url:
            url_time[tv.visit.normalized_url] += tv.estimated_seconds
    source_counts = Counter(v.source for v in visits if v.source)
    source_time = defaultdict(int)
    for tv in timed_visits:
        if tv.visit.source:
            source_time[tv.visit.source] += tv.estimated_seconds

    lines = [
        f'Browser activity report - {day_str}',
        '',
        f'Total unique visits: {total}',
        f'Total estimated browsing time: {format_duration(total_seconds)}',
        '',
        'Input mode: browser activity only',
        f'Activity files scanned: {len(list(ACTIVITY_INPUT_DIR.glob("*.jsonl")))}',
        '',
        'By category (visit count):',
    ]
    if total:
        for cat, count in sorted(category_counts.items(), key=lambda x: (-x[1], x[0])):
            pct = count / total * 100
            lines.append(f'- {cat}: {count} ({pct:.1f}%)')
    else:
        lines.append('- no data')
    lines += ['', 'By category (estimated time):']
    if total_seconds:
        for cat, seconds in sorted(category_time.items(), key=lambda x: (-x[1], x[0])):
            pct = seconds / total_seconds * 100
            lines.append(f'- {cat}: {format_duration(seconds)} ({pct:.1f}%)')
    else:
        lines.append('- no data')
    lines += ['', 'By source:']
    if source_counts:
        for source, count in sorted(source_counts.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f'- {source}: {count} visits, {format_duration(source_time[source])}')
    else:
        lines.append('- no data')
    lines += ['', 'Top domains (visit count):']
    for domain, count in domain_counts.most_common(config.get('top_domains_limit', 15)):
        lines.append(f'- {domain}: {count}')
    if not domain_counts:
        lines.append('- no data')
    lines += ['', 'Top domains (estimated time):']
    for domain, seconds in sorted(domain_time.items(), key=lambda x: (-x[1], x[0]))[:config.get('top_domains_limit', 15)]:
        lines.append(f'- {domain}: {format_duration(seconds)}')
    if not domain_time:
        lines.append('- no data')
    lines += ['', 'Top URLs (visit count):']
    for url, count in url_counts.most_common(config.get('top_urls_limit', 20)):
        lines.append(f'- {url}: {count}')
    if not url_counts:
        lines.append('- no data')
    lines += ['', 'Top URLs (estimated time):']
    for url, seconds in sorted(url_time.items(), key=lambda x: (-x[1], x[0]))[:config.get('top_urls_limit', 20)]:
        lines.append(f'- {url}: {format_duration(seconds)}')
    if not url_time:
        lines.append('- no data')
    lines += ['', 'Sample visits:']
    for tv in timed_visits[:30]:
        v = tv.visit
        lines.append(f'- [{v.visited_at}] {v.category} | {format_duration(tv.estimated_seconds)} | {v.title or "(no title)"} | {v.normalized_url}')
    if not timed_visits:
        lines.append('- no data')
    return '\n'.join(lines) + '\n'


def process_day(day, all_visits: list[Visit], config: dict) -> None:
    """Analyze and write report + data for a single day."""
    day_str = day.isoformat()
    day_visits = filter_by_day(all_visits, day)
    # Include estimated_seconds alongside each Visit so downstream consumers
    # (build_daily_json.py) don't need to re-run session detection.
    timed = estimate_time_spent(day_visits, config)
    enriched = []
    for tv in timed:
        d = asdict(tv.visit)
        d['estimated_seconds'] = tv.estimated_seconds
        enriched.append(d)
    merged_json = OUTPUT_DATA / f'{day_str}.json'
    with merged_json.open('w', encoding='utf-8') as f:
        json.dump(enriched, f, ensure_ascii=False, indent=2)
    report = format_report(day_str, day_visits, config)
    report_path = OUTPUT_DAILY / f'{day_str}.txt'
    report_path.write_text(report, encoding='utf-8')
    print(f'Wrote {report_path} ({len(day_visits)} visits)')


def write_unclassified_domains(visits: list[Visit]) -> None:
    """Append newly seen '其他' domains to ./unclassified_domains.txt (top-level).

    The file lives at the project root next to domain_categories.json so users
    can hand-edit it (or pipe through an LLM) and move keys into the known
    mapping.
    """
    # Load existing entries to avoid duplicates
    existing: set[str] = set()
    if UNCLASSIFIED_DOMAINS_PATH.exists():
        existing = {line.strip() for line in UNCLASSIFIED_DOMAINS_PATH.read_text(encoding='utf-8').splitlines() if line.strip()}

    new_domains = {
        v.domain for v in visits
        if v.category == '其他' and v.domain and not v.domain.startswith('activity.local')
    } - existing

    if new_domains:
        all_domains = sorted(existing | new_domains)
        UNCLASSIFIED_DOMAINS_PATH.parent.mkdir(parents=True, exist_ok=True)
        UNCLASSIFIED_DOMAINS_PATH.write_text('\n'.join(all_domains) + '\n', encoding='utf-8')
        print(f'[analyze] Wrote {len(new_domains)} new unclassified domain(s) to {UNCLASSIFIED_DOMAINS_PATH.name}', file=sys.stderr)


def main() -> None:
    ensure_dirs()
    args = parse_args()
    config = load_json(CONFIG_PATH)
    config['_domain_categories'] = load_domain_categories()

    all_visits_raw = load_all_visits(config)
    deduped_all = dedupe_visits(all_visits_raw)
    # Enrich '其他' categories using Claude API
    deduped_all = enrich_categories_with_claude(deduped_all, config)

    if args.all_days:
        days = get_all_days(deduped_all)
        if not days:
            print('[analyze] No visits found in input files', file=sys.stderr)
            return
        for day in days:
            process_day(day, deduped_all, config)
    else:
        day = resolve_day(args.day or config.get('default_day_mode', 'yesterday'), config.get('timezone', 'Asia/Shanghai'))
        process_day(day, deduped_all, config)

    write_unclassified_domains(deduped_all)


if __name__ == '__main__':
    main()
