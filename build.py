#!/usr/bin/env python3
"""Validate independent contributor models, group earthquakes, build dist/index.html.

Usage: python build.py
Contributors need only place model JSON files into data/models/. They do not
need to coordinate event_id or model_id with other contributors. Source files
are NEVER rewritten. event_registry.json is generated and should be committed.

Records with "status": "draft" are validated (problems are reported as
warnings) but never published. Besides dist/index.html the build writes
machine-readable copies of the published records to dist/data/models.json
and dist/data/<file stem>.json.
"""
import datetime
import hashlib
import json
import math
import os
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime as _EventDateTime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
MODELS_DIR = HERE / 'data' / 'models'
SCHEMA_PATH = HERE / 'schema.json'
TEMPLATE_PATH = HERE / 'template.html'
DIST_PATH = HERE / 'dist' / 'index.html'
REGISTRY_PATH = HERE / 'event_registry.json'
OVERRIDES_PATH = HERE / 'event_overrides.json'
REVIEW_PATH = HERE / 'event_review.json'


# ====== Automatic earthquake matching ======


class MatchError(ValueError):
    def __init__(self, message, review=None):
        super().__init__(message)
        self.review = review or []


def _parse_time(value, source):
    if not isinstance(value, str) or not value.strip():
        raise MatchError(f"{source}: event.origin_time (UTC) is required for automatic matching.")
    try:
        timestamp = _EventDateTime.fromisoformat(value.strip().replace('Z', '+00:00'))
        if timestamp.tzinfo is None:
            raise ValueError('missing timezone')
        return timestamp.astimezone(timezone.utc)
    except ValueError:
        raise MatchError(f"{source}: invalid event.origin_time: {value!r}; provide an ISO 8601 UTC time (e.g. 2017-07-20T22:31:11Z).")


def _coordinates(event):
    h = event.get('hypocenter') or {}
    try:
        lat, lon = float(h.get('latitude')), float(h.get('longitude'))
        if math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            return (lat, lon)
    except (TypeError, ValueError):
        pass
    return None


def _haversine(a, b):
    dlat, dlon = map(math.radians, (b[0] - a[0], b[1] - a[1]))
    la1, la2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin(dlat / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _observation(model, filename):
    event = model.get('event')
    if not isinstance(event, dict):
        raise MatchError(f'{filename}: event object is missing.')
    raw_time = event.get('origin_time')
    # A missing origin time is allowed (historical events); the record then
    # becomes its own event. A malformed non-empty time is still an error.
    missing_time = raw_time is None or (isinstance(raw_time, str) and not raw_time.strip())
    dt = None if missing_time else _parse_time(raw_time, filename)
    coords = _coordinates(event)
    try:
        mag = float(event['magnitude'])
        if not math.isfinite(mag):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise MatchError(f'{filename}: event.magnitude must be a finite number.')
    return {'origin_time_utc': dt.isoformat().replace('+00:00', 'Z') if dt else None,
            'latitude': coords[0] if coords else None,
            'longitude': coords[1] if coords else None,
            'magnitude': mag,
            'name': str(event.get('name') or filename)}


def _similarity(o, anchor):
    if not o.get('origin_time_utc') or not anchor.get('origin_time_utc'):
        return 'different', None, None  # undated records are never merged automatically
    a = _parse_time(o['origin_time_utc'], 'observation')
    b = _parse_time(anchor['origin_time_utc'], 'registry event')
    seconds = abs((a - b).total_seconds())
    if seconds > 300:
        return 'different', seconds, None
    try:
        dmag = abs(float(o['magnitude']) - float(anchor['magnitude']))
    except (TypeError, ValueError, KeyError):
        dmag = math.inf
    p, q = _coordinates({'hypocenter': o}), _coordinates({'hypocenter': anchor})
    if p and q:
        km = _haversine(p, q)
        if km <= 50 and seconds <= 120 and dmag <= 0.5:
            return 'match', seconds, km
        return ('possible' if km <= 150 else 'different'), seconds, km
    return ('possible' if seconds <= 60 and dmag <= 0.5 else 'different'), seconds, None


def _slug(text, limit=40):
    folded = ''.join(c for c in unicodedata.normalize('NFKD', str(text)) if not unicodedata.combining(c))
    folded = ''.join(c if c.isascii() else ('' if c.isalnum() else '-') for c in folded).lower()
    slug = re.sub(r'[^a-z0-9]+', '-', folded).strip('-')
    if len(slug) > limit:  # cut at a word boundary when possible
        slug = slug[:limit + 1].rsplit('-', 1)[0] if '-' in slug[:limit + 1] else slug[:limit]
    return slug.strip('-') or 'event'


def _new_event_key(o, filename, taken):
    base = f"{(o['origin_time_utc'] or '')[:10] or 'undated'}_{_slug(o['name'])}"
    key, digest, n = base, hashlib.sha1(filename.encode('utf-8')).hexdigest()[:6], 1
    while key in taken:
        key = f'{base}-{digest}' if n == 1 else f'{base}-{digest}-{n}'
        n += 1
    return key


def group_models(input_models, registry, overrides):
    """Assign every (filename, model) to an earthquake.

    Returns (resolved, pending_registry, notes): resolved is [(filename, model
    with _browser_* keys)] in filename order. Raises MatchError (with review
    entries) when a record cannot be matched unambiguously and ValueError for
    unusable registry/override contents. Contributor event_id/model_id values
    are informational only and never used here.
    """
    known = registry.get('events') if isinstance(registry, dict) else None
    previous = registry.get('assignments') if isinstance(registry, dict) else None
    if not isinstance(known, dict) or not isinstance(previous, dict) or not all(isinstance(e, dict) for e in known.values()):
        raise ValueError(f'{REGISTRY_PATH.name} must contain "events" and "assignments" objects.')
    if not isinstance(overrides, dict) or not all(isinstance(v, str) for v in overrides.values()):
        raise ValueError(f'{OVERRIDES_PATH.name} must map model filenames to an event key or "separate".')
    filenames = {name for name, _ in input_models}
    notes = [f'{OVERRIDES_PATH.name}: no published model file named {name!r}; entry ignored.'
             for name in sorted(overrides) if name not in filenames]
    # Events of files explicitly kept "separate" never absorb other files automatically.
    private = {previous[n] for n, v in overrides.items() if v == 'separate' and previous.get(n) in known}
    events, assignments, resolved, review = {}, {}, [], []
    for filename, model in sorted(input_models, key=lambda item: item[0]):
        o = _observation(model, filename)
        override, prev = overrides.get(filename), previous.get(filename)
        catalogue = {**known, **events}
        if override == 'separate':
            shared = prev in events or any(v == prev for n, v in previous.items() if n != filename)
            key = prev if prev in known and not shared else _new_event_key(o, filename, catalogue)
            private.add(key)
        elif override is not None:
            if override not in catalogue:
                raise ValueError(f'{OVERRIDES_PATH.name}: {filename} -> unknown event key {override!r}. '
                                 f'Use a key from {REGISTRY_PATH.name} or {REVIEW_PATH.name}, or "separate".')
            key = override
        elif prev in known:
            key = prev  # stable keys: keep the committed assignment
            status, seconds, km = _similarity(o, known[prev])
            if o['origin_time_utc'] and status == 'different':
                detail = 'registry event is undated' if seconds is None else f'{round(seconds)} s apart'
                detail += '' if km is None else f', {round(km)} km apart'
                notes.append(f'{filename}: kept registry event {prev!r} although the record no longer matches it '
                             f'({detail}); add an {OVERRIDES_PATH.name} entry if this is wrong.')
        elif not o['origin_time_utc']:
            key = _new_event_key(o, filename, catalogue)  # no origin_time: not matched automatically
        else:
            found = {k: _similarity(o, ev)[0] for k, ev in catalogue.items() if k not in private}
            matches = sorted(k for k, s in found.items() if s == 'match')
            possible = sorted(k for k, s in found.items() if s == 'possible')
            if len(matches) == 1 and not possible:
                key = matches[0]
            elif not matches and not possible:
                key = _new_event_key(o, filename, catalogue)
            else:
                review.append({'file': filename, 'matches': matches, 'possible_matches': possible})
                continue
        assignments[filename] = key
        event = events.setdefault(key, dict(known.get(key) or o))
        resolved.append((filename, {**model,
                                    '_browser_id': Path(filename).stem,
                                    '_browser_source_file': filename,
                                    '_browser_event_key': key,
                                    '_browser_event_name': str(event.get('name') or o['name']),
                                    '_browser_warnings': []}))
    if review:
        raise MatchError(f'{len(review)} model file(s) could not be matched unambiguously to an earthquake.', review)
    pending = {'version': 1,
               'events': {k: events[k] for k in sorted(events)},
               'assignments': dict(sorted(assignments.items()))}
    return resolved, pending, notes


# ====== JSON input/output ======


def _clip(text, column=None, width=200):
    text = str(text)
    if len(text) <= width:
        return text
    start = max(0, column - 1 - width // 2) if column else 0
    return ('…' if start else '') + text[start:start + width] + ('…' if start + width < len(text) else '')


def _reject_constant(name):
    raise ValueError(f'non-standard JSON value {name} (NaN/Infinity are not allowed)')


def load_json(path):
    """Parse a JSON file. On failure print file/line/column/context and return None."""
    path = Path(path)
    try:
        text = path.read_text(encoding='utf-8-sig')  # tolerate a byte-order mark
    except (OSError, UnicodeError) as e:
        print(f'\nERROR READING: {path.name}\n  {e}')
        return None
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as e:
        print(f'\nINVALID JSON: {path.name}')
        print(f'  Line: {e.lineno}\n  Column: {e.colno}\n  Error: {e.msg}\n  Context:')
        lines = text.splitlines()
        for i in range(max(0, e.lineno - 3), min(len(lines), e.lineno + 2)):
            here = i + 1 == e.lineno
            print(f"  {'>>' if here else '  '} {i + 1}: {_clip(lines[i], e.colno if here else None)}")
        print()
        return None
    except ValueError as e:
        print(f'\nINVALID JSON: {path.name}\n  Error: {e}')
        return None


def optional_json(path, default):
    """Return the parsed file, or `default` if it does not exist. Raise ValueError if it is unusable."""
    path = Path(path)
    if not path.exists():
        return default
    data = load_json(path)
    if data is None:
        raise ValueError(f'{path.name} is not valid JSON (see above); fix or delete it.')
    if not isinstance(data, type(default)):
        raise ValueError(f'{path.name} must contain a JSON {"object" if isinstance(default, dict) else "array"}.')
    return data


def _write_atomic(path, text):
    path = Path(path)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_json_atomic(path, obj):
    _write_atomic(path, json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + '\n')


def _script_json(obj):
    """JSON that is safe to inline in a <script> element."""
    return (json.dumps(obj, ensure_ascii=False).replace('<', '\\u003c')
            .replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))


# ====== Validation ======


_PLACEHOLDER_HOSTS = ('example.org', 'example.com', 'example.net')
_URL_HOST = re.compile(r'\b[a-z][a-z0-9+.-]*://([^/\s?#]+)', re.I)
_ET_AL = re.compile(r'^et\.?\s*al\.?$', re.I)


def _placeholder_url(text):
    for netloc in _URL_HOST.findall(text):
        host = netloc.rsplit('@', 1)[-1].split(':')[0].lower().rstrip('.')
        if host in _PLACEHOLDER_HOSTS or host.endswith(tuple('.' + h for h in _PLACEHOLDER_HOSTS)):
            return True
    return False


def find_placeholders(record):
    """Template placeholders left in a record, as 'at <json path>: ...' messages."""
    found = []

    def walk(value, path):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, path + [k])
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, path + [i])
        elif isinstance(value, str):
            where = '/'.join(map(str, path)) or '(root)'
            if 'REPLACE_WITH' in value:
                found.append(f'at {where}: placeholder text {_clip(value, width=80)!r}')
            elif _placeholder_url(value):
                found.append(f'at {where}: placeholder URL {_clip(value, width=80)!r}')
            if len(path) == 3 and path[:2] == ['reference', 'authors'] and _ET_AL.match(value.strip()):
                found.append(f'at {where}: {value!r} is not an author name; list every author')

    walk(record, [])
    return found


def record_problems(validator, record):
    """Schema/format errors plus placeholder values, as printable messages."""
    errors = sorted(validator.iter_errors(record), key=lambda e: (tuple(str(p) for p in e.absolute_path), e.message))
    problems = [f"at {'/'.join(map(str, e.absolute_path)) or '(root)'}: {_clip(e.message, width=300)}" for e in errors]
    return problems + find_placeholders(record)


def _num(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _f(x, digits=2):
    return f'{round(x, digits):g}'


def _dict(value):
    return value if isinstance(value, dict) else {}


def consistency_warnings(model):
    """Non-fatal data-quality checks; returns short, factual messages."""
    out = []
    event = _dict(model.get('event'))
    segments = [s for s in model.get('fault_segments') or [] if isinstance(s, dict)]
    slips, total_area, has_dims = [], 0.0, False
    for index, seg in enumerate(segments, start=1):
        name = str(seg.get('segment_id') or f'segment {index}')
        length, width = _num(seg.get('length_km')), _num(seg.get('width_km'))
        if length is not None and width is not None:
            total_area, has_dims = total_area + length * width, True
        grid = _dict(seg.get('patch_grid'))
        n_as, n_dd = _num(grid.get('n_along_strike')), _num(grid.get('n_down_dip'))
        p_len, p_wid = _num(grid.get('patch_length_km')), _num(grid.get('patch_width_km'))
        for n, size, total, field in ((n_as, p_len, length, 'length_km'), (n_dd, p_wid, width, 'width_km')):
            if n and size is not None and total is not None and abs(n * size - total) > 0.02 * abs(total):
                out.append(f'{name}: patch grid {n:g} × {_f(size, 3)} km = {_f(n * size)} km but {field} is {_f(total)} km')
        patches = [p for p in seg.get('patches') or [] if isinstance(p, dict)]
        if not patches:
            continue
        if n_as and n_dd and n_as * n_dd != len(patches):
            out.append(f'{name}: patch_grid {n_as:g} × {n_dd:g} = {n_as * n_dd:g} cells but {len(patches)} patches')
        xs = [_num(p.get('along_strike_km')) for p in patches]
        ys = [_num(p.get('down_dip_km')) for p in patches]
        positions = Counter((round(x, 6), round(y, 6)) for x, y in zip(xs, ys) if x is not None and y is not None)
        duplicates = sorted(k for k, c in positions.items() if c > 1)
        if duplicates:
            extra = sum(positions[k] - 1 for k in duplicates)
            out.append(f'{name}: {extra} duplicate patch position(s) (e.g. {duplicates[0][0]:g}/{duplicates[0][1]:g} km)')
        for values, size, n, total, label, field in ((xs, p_len, n_as, length, 'along-strike', 'length'),
                                                      (ys, p_wid, n_dd, width, 'down-dip', 'width')):
            values = [v for v in values if v is not None]
            if not values or total is None:
                continue
            # A centre must lie at least half a patch inside the plane (1 % slack).
            half = abs(size if size is not None else (total / n if n else 0.0)) / 2
            slack = 0.01 * abs(total)
            lo, hi = min(values), max(values)
            if lo < half - slack or hi > total - half + slack:
                out.append(f'{name}: {label} patch centres span {_f(lo)}–{_f(hi)} km but {field} is {_f(total)} km')
        top, dip = _num(seg.get('top_depth_km')), _num(seg.get('dip_deg'))
        if top is not None and dip is not None:
            sin_dip, checked, bad = math.sin(math.radians(dip)), 0, []
            for p, y in zip(patches, ys):
                depth = _num(p.get('depth_km'))
                if depth is None or y is None:
                    continue
                expected = top + y * sin_dip
                checked += 1
                if abs(depth - expected) > max(0.5, 0.05 * abs(expected)):
                    bad.append(abs(depth - expected))
            if bad:
                out.append(f'{name}: {len(bad)}/{checked} patch depths differ from top_depth_km + down_dip·sin(dip) '
                           f'(max {_f(max(bad))} km)')
        slips += [s for s in (_num(p.get('slip_m')) for p in patches) if s is not None]

    metrics = _dict(model.get('summary_metrics'))
    if slips:
        peak, declared = max(slips), _num(metrics.get('max_slip_m'))
        if declared is not None and abs(declared - peak) > 0.02 and abs(declared - peak) > 0.1 * abs(declared):
            out.append(f'summary max_slip_m {_f(declared, 3)} m vs max patch slip {_f(peak, 3)} m')
        declared = _num(metrics.get('mean_slip_m'))
        if declared is not None:
            nonzero = [s for s in slips if s > 0]
            mean_all, mean_nz = sum(slips) / len(slips), (sum(nonzero) / len(nonzero) if nonzero else 0.0)
            if all(abs(declared - m) > 0.15 * abs(declared) for m in (mean_all, mean_nz)):
                out.append(f'summary mean_slip_m {_f(declared, 3)} m vs patch mean {_f(mean_all, 3)} m '
                           f'(non-zero patches {_f(mean_nz, 3)} m)')
    area = _num(metrics.get('rupture_area_km2'))
    if area is not None and has_dims and area > 1.05 * total_area:
        out.append(f'summary rupture_area_km2 {_f(area)} km² exceeds total fault area {_f(total_area)} km²')
    moment, magnitude = _num(metrics.get('seismic_moment_Nm')), _num(event.get('magnitude'))
    if event.get('magnitude_type') == 'Mw' and moment and moment > 0 and magnitude is not None:
        implied = (math.log10(moment) - 9.1) / 1.5
        if abs(magnitude - implied) > 0.3:
            out.append(f'Mw {magnitude:g} but seismic_moment_Nm {moment:.3g} N·m implies Mw {_f(implied)}')

    origin_time = event.get('origin_time')
    if not (isinstance(origin_time, str) and origin_time.strip()):
        out.append('no origin_time: not matched automatically')
    if not _coordinates(event) and not any(_coordinates({'hypocenter': _dict(s.get('reference_point'))}) for s in segments):
        out.append('no hypocenter or reference-point coordinates: not shown on the map')
    license_ = _dict(model.get('provenance')).get('license')
    if not (isinstance(license_, str) and license_.strip()):
        out.append('no license stated (provenance.license)')
    return out


# ====== Build ======


def main():
    schema = load_json(SCHEMA_PATH)
    if schema is None:
        print('BUILD FAILED: missing or invalid schema.json')
        return 1
    try:
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    except Exception as e:
        print(f'INVALID SCHEMA: {e}')
        return 1
    unchecked = [f for f in ('date', 'date-time', 'uri') if f not in Draft202012Validator.FORMAT_CHECKER.checkers]
    if unchecked:
        print(f'NOTE: format checks {unchecked} unavailable; run: pip install -r requirements.txt')

    files = sorted(MODELS_DIR.glob('*.json'))
    print(f'Found {len(files)} JSON model file(s).')
    if not files:
        print('BUILD FAILED: put contributor records into data/models/.')
        return 1
    input_models = []
    syntax_errors = 0
    for index, path in enumerate(files, start=1):
        print(f'Checking [{index}/{len(files)}]: {path.name}')
        raw = load_json(path)
        if raw is None:
            syntax_errors += 1
        else:
            input_models.append((path.name, raw))
    if syntax_errors:
        print(f'BUILD FAILED: {syntax_errors} JSON syntax/read errors. No output generated.')
        return 1

    # Validate before matching, so every record's problems are reported in one run.
    published, drafts = [], []
    schema_errors = 0
    for filename, raw in input_models:
        problems = record_problems(validator, raw)
        if isinstance(raw, dict) and raw.get('status') == 'draft':
            drafts.append(filename)
            notes = consistency_warnings(raw)
            print(f'\nDRAFT (validated, not published): {filename}'
                  + (f' ({len(problems)} problem(s) to fix before publishing)' if problems else ''))
            for no, problem in enumerate(problems, start=1):
                print(f'  [{no}] {problem}')
            for note in notes:
                print(f'  - {note}')
        elif problems:
            schema_errors += len(problems)
            print(f'\nINVALID RECORD: {filename} ({len(problems)} error(s))')
            for no, problem in enumerate(problems, start=1):
                print(f'  [{no}] {problem}')
        else:
            published.append((filename, {k: v for k, v in raw.items() if not k.startswith('_browser_')}))
            print(f'OK: {filename}')
    if schema_errors:
        print(f'\nBUILD FAILED: {schema_errors} validation error(s) (schema, formats, placeholders). No new HTML generated.')
        print('Contributed source files have not been modified.')
        return 1
    if not published:
        print('\nBUILD FAILED: no published records (all are drafts). No new HTML generated.')
        return 1

    try:
        registry = optional_json(REGISTRY_PATH, {'version': 1, 'events': {}, 'assignments': {}})
        overrides = optional_json(OVERRIDES_PATH, {})
        resolved, pending_registry, match_notes = group_models(published, registry, overrides)
    except (MatchError, ValueError) as e:
        print(f'\nEVENT MATCHING NEEDS REVIEW: {e}')
        if isinstance(e, MatchError) and e.review:
            save_json_atomic(REVIEW_PATH, {'review': e.review, 'instructions':
                'Edit event_overrides.json (create it if necessary). Map a filename to an event key from possible_matches, or to "separate". Do not edit contributor JSON.'})
            for r in e.review:
                print(f"  {r['file']}: matches={r['matches']}, possible={r['possible_matches']}")
            print(f'Review details: {REVIEW_PATH}')
        print('No new HTML generated; no potentially incorrect earthquake merging performed.')
        return 1
    for note in match_notes:
        print(f'NOTE: {note}')

    models = []
    for filename, model in resolved:
        model['_browser_warnings'] = model['_browser_warnings'] + consistency_warnings(model)
        models.append(model)
    warning_count = sum(len(m['_browser_warnings']) for m in models)
    warned = [m for m in models if m['_browser_warnings']]
    if warned:
        print('\nDATA-QUALITY WARNINGS (non-fatal; shown in the browser as data-quality notes):')
        for m in warned:
            print(f"  {m['_browser_source_file']}:")
            for warning in m['_browser_warnings']:
                print(f'    - {warning}')

    # Two distinct files may legitimately contain identical contributor model_id
    # values (e.g. two geometry alternatives from the same publication).
    # The browser uses the unique SOURCE FILENAME as its selection key.
    duplicates = {k: n for k, n in Counter(m['model_id'] for m in models).items() if n > 1}
    if duplicates:
        print(f'NOTE: duplicate publication model_id values found; separate variants retained: {duplicates}')
    event_count = len({m['_browser_event_key'] for m in models})
    print(f'\nMatched {len(models)} models into {event_count} distinct earthquakes.')
    print('IDs supplied by contributors are informational, not used for matching.')

    try:
        template = TEMPLATE_PATH.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as e:
        print(f'BUILD FAILED: unable to read template.html: {e}')
        return 1
    required = ['__MODELS_JSON__', '__BUILT_AT__', '__MODEL_COUNT__']
    missing = [s for s in required if s not in template]
    if missing:
        print(f'BUILD FAILED: missing template placeholders {missing}')
        return 1
    # The data goes in last, so placeholder-like text inside records is never replaced.
    document = (template.replace('__BUILT_AT__', datetime.date.today().isoformat())
                .replace('__MODEL_COUNT__', str(len(models)))
                .replace('__MODELS_JSON__', _script_json(models)))
    public = [{k: v for k, v in m.items() if not k.startswith('_browser_')} for m in models]
    data_dir = DIST_PATH.parent / 'data'
    try:
        data_dir.mkdir(exist_ok=True, parents=True)
        written = {'models.json'}
        for model, record in zip(models, public):
            written.add(f"{model['_browser_id']}.json")
            _write_atomic(data_dir / f"{model['_browser_id']}.json", json.dumps(record, indent=2, ensure_ascii=False) + '\n')
        _write_atomic(data_dir / 'models.json', json.dumps(public, ensure_ascii=False) + '\n')
        for stale in data_dir.glob('*.json'):
            if stale.name not in written:
                stale.unlink()
        save_json_atomic(REGISTRY_PATH, pending_registry)
        _write_atomic(DIST_PATH, document)
        REVIEW_PATH.unlink(missing_ok=True)
    except OSError as e:
        print(f'BUILD FAILED writing output: {e}')
        return 1

    print('\nBUILD SUCCESSFUL')
    print(f'Output: {DIST_PATH}')
    print(f'Machine-readable data: {data_dir / "models.json"} + {len(models)} per-record file(s)')
    print(f'Models: {len(models)}')
    print(f'Distinct earthquakes: {event_count}')
    if drafts:
        print(f'Drafts validated but not published: {len(drafts)} ({", ".join(drafts)})')
    print(f'Data-quality warnings: {warning_count} in {len(warned)} model(s) (non-fatal)')
    print(f'Persistent registry: {REGISTRY_PATH} (commit this file to version control)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
