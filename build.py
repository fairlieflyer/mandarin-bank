#!/usr/bin/env python3
"""
build.py: read the Google Sheet, build "Today's Top 30", email it. One file, run daily by GitHub Actions.

Environment variables (GitHub secrets):
  SHEET_URL           published-CSV link of the Bank tab  (or use --csv path/to/file.csv)
  GMAIL_USER          Gmail address to send from
  GMAIL_APP_PASSWORD  Google app password (16 characters)
  MAIL_TO             recipient (optional; defaults to GMAIL_USER)

Usage
  python build.py --csv bank_test.csv --dry-run     # build only: writes out/top30_<date>.html, sends nothing
  python build.py --force                           # build and send now (ignores the 5am gate)
  python build.py                                   # scheduled mode: sends once a day, from 5am Zurich time
"""
import argparse, csv, datetime as dt, html, io, json, math, os, random, re, smtplib, sys, unicodedata, urllib.request
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from pypinyin import lazy_pinyin, Style

TOTAL, NEW_PER_DAY, REVIEW_MAX, MIN_MEASURE = 30, 10, 10, 6
REVIEW_DAYS = (1, 3, 7, 14, 30)
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state.json')
TZ = ZoneInfo('Europe/Zurich')


def key(zh):                                   # identity of a row = its Hanzi without spaces/punctuation
    return re.sub(r'[\s，。？！、,.?!…（）()/；;：:"“”]', '', zh)


def norm(h):                                   # "Hànzì" -> "hanzi"
    return unicodedata.normalize('NFKD', h).encode('ascii', 'ignore').decode().strip().lower()


def auto_pinyin(zh):
    s = ' '.join(lazy_pinyin(zh, style=Style.TONE))
    return re.sub(r'\s+([，。？！、,.?!；;：:])', r'\1', s)


def parse_date(s):
    s = (s or '').strip()
    for f in ('%Y-%m-%d', '%d/%m/%Y', '%d.%m.%Y', '%d %b %Y'):
        try: return dt.datetime.strptime(s, f).date()
        except ValueError: pass
    return None


def read_rows(args):
    if args.csv: text = open(args.csv, encoding='utf-8-sig').read()
    else:
        url = os.environ.get('SHEET_URL', '').strip()
        if not url: sys.exit('ERROR: SHEET_URL is not set (publish the Bank tab as CSV and add the link as a secret).')
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            text = urllib.request.urlopen(req, timeout=30).read().decode('utf-8-sig')
        except Exception as e:
            sys.exit(f'ERROR: could not download the sheet: {type(e).__name__}: {e}')
    if text.lstrip().lower().startswith('<!doctype html') or '<html' in text[:200].lower():
        sys.exit('ERROR: SHEET_URL returned a web page, not CSV. Check the sheet is published to the web as CSV.')
    reader = csv.reader(io.StringIO(text))
    cols = {norm(h): i for i, h in enumerate(next(reader, []))}
    for need in ('english', 'hanzi'):
        if need not in cols: sys.exit(f'ERROR: the sheet has no "{need}" column. Headers found: {sorted(cols)}')
    get = lambda row, name: (row[cols[name]].strip() if name in cols and cols[name] < len(row) else '')
    rows, seen = [], set()
    for row in reader:
        en, zh = get(row, 'english'), get(row, 'hanzi')
        if not en or not zh or key(zh) in seen: continue
        seen.add(key(zh))
        typ = (get(row, 'type') or 'sentence').lower()
        rows.append(dict(key=key(zh), en=en, zh=zh, py=get(row, 'pinyin') or auto_pinyin(zh), type=typ,
                         topic=get(row, 'topic'), grammar=get(row, 'grammar'), mw=get(row, 'measure word'),
                         tricky=get(row, 'tricky').lower() in ('x', 'y', 'yes', '1', 'true'),
                         start=parse_date(get(row, 'start date'))))
    if not rows: sys.exit('ERROR: the sheet has no usable rows (need English and Hànzì).')
    return rows


def weighted_sample(rng, items, weight, k):    # random sample without replacement, weighted
    return sorted(items, key=lambda r: rng.random() ** (1.0 / weight(r)), reverse=True)[:k]


def build(rows, state, today, rng):
    intro, t = state['introduced'], today.isoformat()
    active = [r for r in rows if r['type'] != 'pattern' and (r['start'] is None or r['start'] <= today)]

    # 1) NEW: introduce the next rows in sheet order, up to NEW_PER_DAY a day
    have = sum(1 for r in active if intro.get(r['key']) == t)
    for r in [r for r in active if r['key'] not in intro][:max(0, NEW_PER_DAY - have)]: intro[r['key']] = t
    new = [r for r in active if intro.get(r['key']) == t]

    # 2) REVIEW: rows first introduced 1, 3, 7, 14 or 30 days ago
    age = lambda r: (today - dt.date.fromisoformat(intro[r['key']])).days
    due = [r for r in active if r['key'] in intro and age(r) in REVIEW_DAYS]
    review = rng.sample(due, min(REVIEW_MAX, len(due)))

    # 3) MIXED: fill to TOTAL from everything introduced. Tricky x3, words x0.5, big topics down-weighted.
    used = {r['key'] for r in new + review}
    pool = [r for r in active if r['key'] in intro and r['key'] not in used]
    size = {}
    for r in pool: size[r['topic']] = size.get(r['topic'], 0) + 1
    w = lambda r: (3 if r['tricky'] else 1) * (0.5 if r['type'] == 'word' else 1) / math.sqrt(size[r['topic']])
    mixed = weighted_sample(rng, pool, w, TOTAL - len(new) - len(review))

    # guarantee at least MIN_MEASURE measure-word items (a non-个 measure word)
    real = lambda r: r['mw'] and not r['mw'].startswith('个')
    short = MIN_MEASURE - sum(1 for r in new + review + mixed if real(r))
    if short > 0:
        chosen = {r['key'] for r in mixed}
        adds = weighted_sample(rng, [r for r in pool if real(r) and r['key'] not in chosen], lambda r: 1, short)
        drop = {r['key'] for r in [r for r in mixed if not real(r)][:len(adds)]}
        mixed = [r for r in mixed if r['key'] not in drop] + adds
    rng.shuffle(mixed)

    # 4) GRAMMAR SPOTLIGHT: one label a day, cycling through the labels in the sheet
    labels = sorted({r['grammar'] for r in active if r['grammar'] and r['key'] in intro
                     and sum(1 for x in active if x['grammar'] == r['grammar']) >= 3})
    spot = None
    if labels:
        label = labels[today.toordinal() % len(labels)]
        ex = [r for r in active if r['grammar'] == label]
        sent = [r for r in ex if r['type'] == 'sentence']
        spot = (label, rng.sample(sent if len(sent) >= 3 else ex, min(3, len(ex))))
    return new, review, mixed, spot


def render(today, new, review, mixed, spot):
    e, n = html.escape, 0
    pretty = f'{today.strftime("%A")} {today.day} {today.strftime("%B %Y")}'
    td = 'style="border:1px solid #cfd8e3;padding:8px 10px;vertical-align:top;"'
    tdzh = 'style="border:1px solid #cfd8e3;padding:8px 10px;vertical-align:top;font-size:20px;"'
    th = 'style="border:1px solid #cfd8e3;padding:8px 10px;background:#eaf1f8;text-align:left;"'

    def tag(r):
        t = r['topic'] + (f' — {r["grammar"]}' if r['grammar'] else '')
        return t + (f' · measure word {r["mw"]}' if r['mw'] else '')

    def section(title, items):
        nonlocal n
        if not items: return ''
        out = f'<tr><td colspan="5" style="padding:10px;background:#2c5f8a;color:#fff;font-weight:bold;">{e(title)} ({len(items)})</td></tr>'
        for r in items:
            n += 1
            out += (f'<tr><td {td} align="center">{n}</td><td {td}>{e(r["en"])}</td>'
                    f'<td {tdzh}>{e(r["zh"])}</td>'
                    f'<td {td}>{e(r["py"])}</td><td {td}><span style="font-size:13px;color:#5b6b7b;">{e(tag(r))}</span></td></tr>')
        return out

    body = section('New today', new) + section('Review', review) + section('Mixed practice', mixed)
    gram = ''
    if spot:
        gram = (f'<h3 style="color:#2c5f8a;">Grammar spotlight: {e(spot[0])}</h3><table style="border-collapse:collapse;width:100%;">'
                f'<tr><th {th}>English</th><th {th}>Hànzì (汉字)</th><th {th}>Pinyin</th></tr>'
                + ''.join(f'<tr><td {td}>{e(r["en"])}</td><td {td}>{e(r["zh"])}</td><td {td}>{e(r["py"])}</td></tr>' for r in spot[1])
                + '</table>')
    page = (f'<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:980px;margin:0 auto;padding:16px;color:#222;">'
            f'<h1 style="text-align:center;color:#2c5f8a;margin-bottom:4px;">Today\'s Top {n}</h1>'
            f'<p style="text-align:center;color:#555;">{pretty}</p>'
            f'<div style="border-left:4px solid #2c5f8a;background:#eaf1f8;padding:12px 16px;margin:16px 0;">'
            f'<strong>The one rule:</strong> say each sentence aloud from the English first, then check.</div>'
            f'<table style="border-collapse:collapse;width:100%;"><tr><th {th}>#</th><th {th}>English</th><th {th}>Hànzì (汉字)</th>'
            f'<th {th}>Pinyin</th><th {th}>Testing / from</th></tr>{body}</table>{gram}</body></html>')
    return page, n, pretty


def send(page, pretty, n):
    user = os.environ.get('GMAIL_USER', '').strip()
    pw = os.environ.get('GMAIL_APP_PASSWORD', '').replace(' ', '').strip()
    to = os.environ.get('MAIL_TO', '').strip() or user
    missing = [k for k, v in (('GMAIL_USER', user), ('GMAIL_APP_PASSWORD', pw)) if not v]
    if missing: sys.exit('ERROR: missing secret(s): ' + ', '.join(missing))
    msg = EmailMessage()
    msg['Subject'], msg['From'], msg['To'] = f"Today's Top {n}: {pretty}", user, to
    msg.set_content('Your Mandarin Top 30 is in the HTML part of this email. Open it in a mail app that shows HTML.')
    msg.add_alternative(page, subtype='html')
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30) as s:
            s.login(user, pw); s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        sys.exit(f'ERROR: Gmail rejected the login ({e.smtp_code}). GMAIL_APP_PASSWORD must be an app password (needs 2-step verification).')
    except Exception as e:
        sys.exit(f'ERROR: sending failed: {type(e).__name__}: {e}')
    print(f'Sent "{msg["Subject"]}" to {to}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', help='read this CSV instead of SHEET_URL (for testing)')
    ap.add_argument('--dry-run', action='store_true', help='build only: write the HTML, send nothing, save nothing')
    ap.add_argument('--force', action='store_true', help='ignore the 5am gate and the "already sent today" check')
    ap.add_argument('--date', help='pretend today is YYYY-MM-DD (testing)')
    args = ap.parse_args()

    now = dt.datetime.now(TZ)
    today = dt.date.fromisoformat(args.date) if args.date else now.date()
    state = json.load(open(STATE, encoding='utf-8')) if os.path.exists(STATE) else {'last_sent': '', 'introduced': {}}

    if not (args.force or args.dry_run):           # scheduled mode: cron fires twice a day to cover summer/winter time
        if now.hour < 5: print('Before 5am Zurich time: skipping (the other cron slot will send).'); return
        if state.get('last_sent') == today.isoformat(): print('Already sent today: skipping.'); return

    rows = read_rows(args)
    new, review, mixed, spot = build(rows, state, today, random.Random(today.isoformat()))
    page, n, pretty = render(today, new, review, mixed, spot)
    os.makedirs('out', exist_ok=True)
    path = os.path.join('out', f'top30_{today.isoformat()}.html')
    open(path, 'w', encoding='utf-8').write(page)
    real = sum(1 for r in new + review + mixed if r['mw'] and not r['mw'].startswith('个'))
    print(f'Built {path}: {n} items ({len(new)} new, {len(review)} review, {len(mixed)} mixed; {real} measure-word; '
          f'{len(rows)} rows in the sheet)')
    if args.dry_run: return
    send(page, pretty, n)
    state['last_sent'] = today.isoformat()
    json.dump(state, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))


if __name__ == '__main__':
    main()
