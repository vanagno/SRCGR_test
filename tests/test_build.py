"""Tests for build.py (run from the repository root: python -m pytest -q).

Every test that runs the build redirects its outputs (dist/, event_registry.json,
event_review.json, event_overrides.json) to a temporary directory, so the
repository itself is never written to.
"""
import copy
import importlib.util
import json
import math
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]

sys.dont_write_bytecode = True  # keep the repository free of __pycache__ from build.py
_spec = importlib.util.spec_from_file_location('srcgr_build', ROOT / 'build.py')
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

TEMPLATE = ('<!doctype html><title>test</title><p>Built __BUILT_AT__ with __MODEL_COUNT__ models</p>\n'
            '<script>const MODELS = __MODELS_JSON__;</script>\n')


def _patch(x, y, slip, dip=45.0, top=0.0):
    return {'along_strike_km': x, 'down_dip_km': y, 'latitude': None, 'longitude': None,
            'depth_km': round(top + y * math.sin(math.radians(dip)), 3), 'slip_m': slip, 'rake_deg': -90.0}


def make_record(name='Test earthquake', time='2020-01-01T00:00:00Z', lat=38.0, lon=23.0, mag=6.0):
    """A small, schema-valid, internally consistent record (no warnings)."""
    return {
        'schema_version': '1.0.0',
        'model_id': 'testquake2020_tester_2021',
        'event': {
            'event_id': 'testquake2020',
            'name': name,
            'origin_time': time,
            'magnitude': mag,
            'magnitude_type': 'Mw',
            'hypocenter': {'latitude': lat, 'longitude': lon, 'depth_km': 1.0},
            'faulting_style': 'normal',
        },
        'reference': {'authors': ['A. Tester'], 'year': 2021, 'title': 'A test model',
                      'doi': '10.1234/test.2021', 'url': 'https://doi.org/10.1234/test.2021'},
        'fault_segments': [{
            'segment_id': 'seg1', 'strike_deg': 90.0, 'dip_deg': 45.0, 'length_km': 2.0, 'width_km': 2.0,
            'top_depth_km': 0.0, 'reference_point': {'latitude': lat, 'longitude': lon},
            'patch_grid': {'n_along_strike': 2, 'n_down_dip': 2, 'patch_length_km': 1.0, 'patch_width_km': 1.0},
            'patches': [_patch(0.5, 0.5, 1.0), _patch(1.5, 0.5, 0.5), _patch(0.5, 1.5, 1.0), _patch(1.5, 1.5, 0.5)],
        }],
        'summary_metrics': {'max_slip_m': 1.0, 'mean_slip_m': 0.75},
        'provenance': {'license': 'CC-BY-4.0'},
    }


def empty_registry():
    return {'version': 1, 'events': {}, 'assignments': {}}


@pytest.fixture
def site(tmp_path, monkeypatch):
    """Point every build.py path at a temporary site (schema.json is read from the repo)."""
    models = tmp_path / 'models'
    models.mkdir()
    template = tmp_path / 'template.html'
    template.write_text(TEMPLATE, encoding='utf-8')
    paths = {'MODELS_DIR': models, 'TEMPLATE_PATH': template, 'DIST_PATH': tmp_path / 'dist' / 'index.html',
             'REGISTRY_PATH': tmp_path / 'event_registry.json', 'OVERRIDES_PATH': tmp_path / 'event_overrides.json',
             'REVIEW_PATH': tmp_path / 'event_review.json'}
    for name, value in paths.items():
        monkeypatch.setattr(build, name, value)

    def add(filename, record):
        (models / filename).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')

    def blob():
        html = paths['DIST_PATH'].read_text(encoding='utf-8')
        return re.search(r'const MODELS = (.*);</script>', html).group(1)

    return SimpleNamespace(add=add, blob=blob, dist=paths['DIST_PATH'], data=paths['DIST_PATH'].parent / 'data',
                           registry=paths['REGISTRY_PATH'], overrides=paths['OVERRIDES_PATH'],
                           review=paths['REVIEW_PATH'], template=template)


# ====== Event matching ======


@pytest.mark.parametrize('other, expected', [
    (dict(time='2020-01-01T00:00:30Z', lat=38.05, lon=23.05, mag=6.2), 'match'),     # ~7 km, 30 s
    (dict(time='2020-01-01T00:03:00Z', lat=38.0, lon=23.0), 'possible'),               # 180 s apart
    (dict(time='2020-01-01T00:00:10Z', lat=38.9, lon=23.0), 'possible'),               # ~100 km
    (dict(time='2020-01-01T00:00:10Z', lat=40.0, lon=23.0), 'different'),              # ~220 km
    (dict(time='2020-01-01T00:06:00Z'), 'different'),                                  # > 300 s
    (dict(time='2020-01-01T00:00:10Z', mag=7.0), 'possible'),                          # |dM| > 0.5
    (dict(time='2020-01-01T00:00:30Z', lat=None, lon=None), 'possible'),               # no coordinates
    (dict(time='2020-01-01T00:01:30Z', lat=None, lon=None), 'different'),              # no coords, > 60 s
    (dict(time=None), 'different'),                                                    # undated
])
def test_similarity_thresholds(other, expected):
    a = build._observation(make_record(), 'a.json')
    b = build._observation(make_record(**other), 'b.json')
    assert build._similarity(b, a)[0] == expected


def test_same_event_merges():
    models = [('A.json', make_record()),
              ('B.json', make_record(name='Same quake, other agency', time='2020-01-01T00:00:20Z', lat=38.1, mag=6.1))]
    resolved, registry, notes = build.group_models(models, empty_registry(), {})
    keys = {m['_browser_event_key'] for _, m in resolved}
    assert len(keys) == 1
    assert list(registry['events']) == ['2020-01-01_test-earthquake']
    assert registry['assignments'] == {'A.json': '2020-01-01_test-earthquake', 'B.json': '2020-01-01_test-earthquake'}
    assert [m['_browser_id'] for _, m in resolved] == ['A', 'B']
    assert notes == []


def test_far_apart_and_different_days_stay_separate():
    models = [('A.json', make_record()),
              ('B.json', make_record(lat=35.0, lon=26.0)),                 # same time, ~430 km away
              ('C.json', make_record(time='2021-01-01T00:00:00Z'))]        # a year later
    resolved, registry, _ = build.group_models(models, empty_registry(), {})
    assert len({m['_browser_event_key'] for _, m in resolved}) == 3
    # identical name and date: the second key gets a filename-hash suffix
    assert registry['assignments']['B.json'].startswith('2020-01-01_test-earthquake-')


def test_ambiguous_match_raises_with_review():
    models = [('A.json', make_record()), ('B.json', make_record(lat=38.9))]  # ~100 km: possible
    with pytest.raises(build.MatchError) as info:
        build.group_models(models, empty_registry(), {})
    assert info.value.review == [{'file': 'B.json', 'matches': [], 'possible_matches': ['2020-01-01_test-earthquake']}]


def test_ambiguous_build_writes_review_and_no_output(site, capsys):
    site.add('A.json', make_record())
    site.add('B.json', make_record(lat=38.9))
    assert build.main() == 1
    assert 'EVENT MATCHING NEEDS REVIEW' in capsys.readouterr().out
    assert json.loads(site.review.read_text())['review'][0]['file'] == 'B.json'
    assert not site.dist.exists() and not site.registry.exists()

    site.overrides.write_text(json.dumps({'B.json': 'separate'}))
    assert build.main() == 0
    registry = json.loads(site.registry.read_text())
    assert registry['assignments']['A.json'] != registry['assignments']['B.json']
    assert not site.review.exists()


def test_override_separate_and_join():
    models = [('A.json', make_record()), ('B.json', make_record(lat=38.9))]
    resolved, _, _ = build.group_models(models, empty_registry(), {'B.json': 'separate'})
    assert len({m['_browser_event_key'] for _, m in resolved}) == 2
    resolved, _, _ = build.group_models(models, empty_registry(), {'B.json': '2020-01-01_test-earthquake'})
    assert len({m['_browser_event_key'] for _, m in resolved}) == 1
    # A file kept separate is never merged into, even by a perfect match.
    models.append(('C.json', make_record()))
    resolved, _, _ = build.group_models(models, empty_registry(), {'A.json': 'separate', 'B.json': 'separate'})
    assert len({m['_browser_event_key'] for _, m in resolved}) == 3


def test_override_unknown_key_and_unknown_file():
    models = [('A.json', make_record())]
    with pytest.raises(ValueError, match='unknown event key'):
        build.group_models(models, empty_registry(), {'A.json': 'no-such-event'})
    _, _, notes = build.group_models(models, empty_registry(), {'gone.json': 'separate'})
    assert any('gone.json' in n for n in notes)


def test_missing_origin_time_is_its_own_event(site, capsys):
    models = [('A.json', make_record()), ('B.json', make_record(time=None))]
    resolved, registry, _ = build.group_models(models, empty_registry(), {})
    assert registry['assignments']['B.json'].startswith('undated_')
    assert len({m['_browser_event_key'] for _, m in resolved}) == 2
    assert 'no origin_time: not matched automatically' in build.consistency_warnings(models[1][1])

    for name, record in models:
        site.add(name, record)
    assert build.main() == 0
    assert 'no origin_time: not matched automatically' in capsys.readouterr().out


def test_malformed_origin_time_is_reported():
    record = make_record(time='yesterday')
    with pytest.raises(build.MatchError, match='invalid event.origin_time'):
        build.group_models([('A.json', record)], empty_registry(), {})


def test_registry_keys_are_stable():
    models = [('A.json', make_record()), ('B.json', make_record(time='2021-01-01T00:00:00Z'))]
    _, registry, _ = build.group_models(models, empty_registry(), {})
    # A contributor renames a file and fixes the event name: the earthquake keeps its key.
    renamed = [('A_renamed.json', make_record(name='Renamed quake')), models[1]]
    resolved, again, _ = build.group_models(renamed, copy.deepcopy(registry), {})
    assert again['assignments']['A_renamed.json'] == registry['assignments']['A.json']
    assert 'A.json' not in again['assignments']
    # Re-running on its own output is a no-op.
    _, third, _ = build.group_models(renamed, copy.deepcopy(again), {})
    assert third == again


# ====== Validation ======


def test_placeholders_are_rejected(site, capsys):
    record = make_record()
    record['reference']['title'] = 'REPLACE_WITH_TITLE'
    record['reference']['url'] = 'https://example.org/replace-me'
    record['reference']['authors'] = ['A. Tester', 'et al.']
    assert sorted(p.split(':')[0] for p in build.find_placeholders(record)) == [
        'at reference/authors/1', 'at reference/title', 'at reference/url']
    site.add('A.json', record)
    assert build.main() == 1
    out = capsys.readouterr().out
    assert 'at reference/title' in out and 'at reference/url' in out and 'at reference/authors/1' in out
    assert not site.dist.exists() and not site.registry.exists()


def test_drafts_are_validated_but_not_published(site, capsys):
    draft = make_record(name='Draft quake', time='2010-01-01T00:00:00Z')
    draft['status'] = 'draft'
    draft['reference']['title'] = 'REPLACE_WITH_TITLE'
    site.add('A.json', make_record())
    site.add('Draft.json', draft)
    assert build.main() == 0
    out = capsys.readouterr().out
    assert 'DRAFT (validated, not published): Draft.json' in out and 'reference/title' in out
    published = json.loads((site.data / 'models.json').read_text())
    assert [m['event']['name'] for m in published] == ['Test earthquake']
    assert not (site.data / 'Draft.json').exists()
    assert 'Draft.json' not in json.loads(site.registry.read_text())['assignments']


@pytest.mark.parametrize('field, value', [
    (('event', 'origin_time'), '2020-01-01 noon'),
    (('event', 'origin_time'), '2020-13-01T00:00:00Z'),
    (('provenance', 'date_added'), '2026-02-30'),
])
def test_format_checking(site, capsys, field, value):
    record = make_record()
    record[field[0]][field[1]] = value
    site.add('A.json', record)
    assert build.main() == 1
    out = capsys.readouterr().out
    assert f'at {field[0]}/{field[1]}' in out and 'is not a' in out
    assert not site.dist.exists()


def test_invalid_json_reports_location(site, capsys):
    site.add('A.json', make_record())
    (build.MODELS_DIR / 'B.json').write_text('{\n  "a": 1,\n  "b": [1, 2,]\n}\n', encoding='utf-8')
    assert build.main() == 1
    out = capsys.readouterr().out
    assert 'INVALID JSON: B.json' in out and 'Line: 3' in out and '>> 3:' in out
    assert not site.dist.exists()


def test_clean_record_has_no_warnings():
    assert build.consistency_warnings(make_record()) == []


def test_consistency_warning_grid_mismatch(site, capsys):
    record = make_record()
    record['fault_segments'][0]['patch_grid']['n_along_strike'] = 3
    site.add('A.json', record)
    assert build.main() == 0
    assert 'seg1: patch_grid 3 × 2 = 6 cells but 4 patches' in capsys.readouterr().out
    model = json.loads(site.blob())[0]
    assert 'seg1: patch_grid 3 × 2 = 6 cells but 4 patches' in model['_browser_warnings']


def test_consistency_checks_catch_inconsistent_metadata():
    record = make_record()
    seg = record['fault_segments'][0]
    seg['patches'][3] = dict(seg['patches'][0])                 # duplicate position
    seg['patches'][1]['down_dip_km'] = 3.5                      # outside the 2 km wide plane
    seg['patches'][2]['depth_km'] = 9.0                         # off the plane
    record['summary_metrics'].update(max_slip_m=2.0, mean_slip_m=0.2, rupture_area_km2=50.0,
                                     seismic_moment_Nm=1e17)
    record['provenance']['license'] = None
    joined = '\n'.join(build.consistency_warnings(record))
    for expected in ('duplicate patch position', 'down-dip patch centres span', 'patch depths differ',
                     'summary max_slip_m 2 m', 'summary mean_slip_m 0.2 m', 'rupture_area_km2 50',
                     'implies Mw', 'no license'):
        assert expected in joined


# ====== Output ======


def test_script_escaping_and_placeholder_text_survive(site):
    name = '</script><script>window.x=1</script> __BUILT_AT__ __MODEL_COUNT__ __MODELS_JSON__    <!--'
    site.add('A.json', make_record(name=name))
    assert build.main() == 0
    html = site.dist.read_text(encoding='utf-8')
    blob = site.blob()
    assert '<' not in blob and ' ' not in blob and ' ' not in blob
    assert html.count('</script>') == 1
    assert 'Built __BUILT_AT__' not in html and 'with 1 models' in html
    model = json.loads(blob)[0]
    assert model['event']['name'] == name
    assert model['_browser_event_name'] == name
    assert json.loads((site.data / 'A.json').read_text(encoding='utf-8'))['event']['name'] == name


def test_outputs_and_determinism(site):
    site.add('B.json', make_record(time='2021-01-01T00:00:00Z'))
    site.add('A.json', make_record())
    site.data.mkdir(parents=True)
    (site.data / 'Stale.json').write_text('{}')
    assert build.main() == 0
    first = site.registry.read_bytes()
    assert build.main() == 0
    assert site.registry.read_bytes() == first and first.endswith(b'}\n')
    models = json.loads(site.blob())
    assert [m['_browser_id'] for m in models] == ['A', 'B']
    assert {m['_browser_source_file'] for m in models} == {'A.json', 'B.json'}
    public = json.loads((site.data / 'models.json').read_text())
    assert len(public) == 2 and not any(k.startswith('_browser_') for m in public for k in m)
    assert sorted(p.name for p in site.data.iterdir()) == ['A.json', 'B.json', 'models.json']
    assert json.loads((site.data / 'A.json').read_text()) == public[0]


def test_real_repository_builds(tmp_path, monkeypatch, capsys):
    """Smoke test: the committed data, schema and template build cleanly (outputs go to tmp)."""
    for name in ('event_registry.json', 'event_overrides.json'):
        if (ROOT / name).exists():
            shutil.copy(ROOT / name, tmp_path / name)
    monkeypatch.setattr(build, 'DIST_PATH', tmp_path / 'dist' / 'index.html')
    monkeypatch.setattr(build, 'REGISTRY_PATH', tmp_path / 'event_registry.json')
    monkeypatch.setattr(build, 'OVERRIDES_PATH', tmp_path / 'event_overrides.json')
    monkeypatch.setattr(build, 'REVIEW_PATH', tmp_path / 'event_review.json')
    assert build.main() == 0, capsys.readouterr().out
    files = sorted((ROOT / 'data' / 'models').glob('*.json'))
    published = [f for f in files if json.loads(f.read_text(encoding='utf-8')).get('status') != 'draft']
    public = json.loads((tmp_path / 'dist' / 'data' / 'models.json').read_text(encoding='utf-8'))
    assert len(public) == len(published) > 0
    html = (tmp_path / 'dist' / 'index.html').read_text(encoding='utf-8')
    assert '__MODELS_JSON__' not in html and '__MODEL_COUNT__' not in html
