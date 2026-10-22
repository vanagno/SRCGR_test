"""Tests for build.py (run from the repository root: python -m pytest -q).

Every test that runs the build redirects its outputs (dist/, event_registry.json,
event_review.json, event_overrides.json) to a temporary directory, so the
repository itself is never written to.
"""
import copy
import hashlib
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

SCHEMA = json.loads((ROOT / 'schema.json').read_text(encoding='utf-8'))
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


# Compared with make_record() (2020-01-01T00:00:00Z, 38.0/23.0, Mw 6.0). Latitude offsets:
# 0.179° ≈ 19.9 km, 0.181° ≈ 20.1 km, 0.449° ≈ 49.9 km, 0.451° ≈ 50.1 km. All limits are inclusive.
@pytest.mark.parametrize('other, expected', [
    # Both located: 'match' only within 5 s, 20 km and 0.5 magnitude units; otherwise 'possible' up to 50 km.
    (dict(), 'match'),
    (dict(time='2020-01-01T00:00:05Z', lat=38.179, mag=6.5), 'match'),
    (dict(time='2020-01-01T00:00:05Z', lat=38.179, mag=5.5), 'match'),
    (dict(time='2020-01-01T00:00:06Z'), 'possible'),                                    # > 5 s
    (dict(lat=38.181), 'possible'),                                                      # > 20 km
    (dict(mag=6.6), 'possible'),                                                         # |dM| > 0.5
    (dict(time='2020-01-01T00:05:00Z', lat=38.449, mag=7.5), 'possible'),                # 300 s, 49.9 km, |dM| 1.5
    (dict(lat=38.451), 'different'),                                                     # > 50 km
    (dict(lat=40.0), 'different'),                                                       # ~220 km
    (dict(time='2020-01-01T00:05:01Z'), 'different'),                                    # > 300 s
    (dict(mag=7.6), 'different'),                                                        # |dM| > 1.5
    (dict(mag=4.4), 'different'),
    # One side without coordinates: 'match' only within 1 s and 0.4 magnitude units.
    (dict(lat=None, lon=None), 'match'),
    (dict(time='2020-01-01T00:00:01Z', lat=None, lon=None, mag=6.4), 'match'),           # 6.4 - 6.0 counts as 0.4
    (dict(time='2020-01-01T00:00:01Z', lat=None, lon=None, mag=5.6), 'match'),
    (dict(time='2020-01-01T00:00:01.5Z', lat=None, lon=None), 'possible'),                # > 1 s
    (dict(time='2020-01-01T00:00:03Z', lat=None, lon=None), 'possible'),
    (dict(lat=None, lon=None, mag=6.5), 'possible'),                                     # |dM| > 0.4
    (dict(time='2020-01-01T00:05:00Z', lat=None, lon=None, mag=7.5), 'possible'),
    (dict(time='2020-01-01T00:05:01Z', lat=None, lon=None), 'different'),
    (dict(lat=None, lon=None, mag=7.6), 'different'),
    (dict(time=None), 'different'),                                                     # undated: never matched
    (dict(time=None, lat=None, lon=None), 'different'),
])
def test_similarity_thresholds(other, expected):
    a = build._observation(make_record(), 'a.json')
    b = build._observation(make_record(**other), 'b.json')
    assert build._similarity(b, a)[0] == build._similarity(a, b)[0] == expected


def test_similarity_reports_time_and_distance():
    a = build._observation(make_record(), 'a.json')
    status, seconds, km = build._similarity(build._observation(make_record(time='2020-01-01T00:00:05Z', lat=38.179), 'b.json'), a)
    assert status == 'match' and seconds == 5.0 and km == pytest.approx(19.904, abs=0.001)
    assert build._similarity(build._observation(make_record(lat=None, lon=None), 'b.json'), a) == ('match', 0.0, None)
    assert build._similarity(build._observation(make_record(time=None), 'b.json'), a) == ('different', None, None)


def test_same_event_merges():
    models = [('A.json', make_record()),
              ('B.json', make_record(name='Same quake, other agency', time='2020-01-01T00:00:03Z', lat=38.1, mag=6.1))]
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


@pytest.mark.parametrize('other', [dict(lat=38.3),                                         # ~33 km apart
                                   dict(time='2020-01-01T00:00:30Z'),                      # 30 s apart
                                   dict(time='2020-01-01T00:00:03Z', lat=None, lon=None)])  # 3 s, no coordinates
def test_ambiguous_match_raises_with_review(other):
    models = [('A.json', make_record()), ('B.json', make_record(**other))]  # close, but not a sure match
    with pytest.raises(build.MatchError) as info:
        build.group_models(models, empty_registry(), {})
    assert info.value.review == [{'file': 'B.json', 'matches': [], 'possible_matches': ['2020-01-01_test-earthquake']}]


def test_ambiguous_build_writes_review_and_no_output(site, capsys):
    site.add('A.json', make_record())
    site.add('B.json', make_record(lat=38.3))
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
    models = [('A.json', make_record()), ('B.json', make_record(lat=38.3))]  # ~33 km: possible
    resolved, _, _ = build.group_models(models, empty_registry(), {'B.json': 'separate'})
    assert len({m['_browser_event_key'] for _, m in resolved}) == 2
    resolved, _, _ = build.group_models(models, empty_registry(), {'B.json': '2020-01-01_test-earthquake'})
    assert len({m['_browser_event_key'] for _, m in resolved}) == 1
    # A file kept separate is never merged into automatically; even a perfect match goes to review.
    models.append(('C.json', make_record()))
    with pytest.raises(build.MatchError) as info:
        build.group_models(models, empty_registry(), {'A.json': 'separate', 'B.json': 'separate'})
    [entry] = info.value.review
    assert entry['file'] == 'C.json' and entry['matches'] == [] and len(entry['possible_matches']) == 2
    resolved, _, _ = build.group_models(models, empty_registry(),
                                        {'A.json': 'separate', 'B.json': 'separate', 'C.json': 'separate'})
    assert len({m['_browser_event_key'] for _, m in resolved}) == 3


def test_separate_override_leaves_a_shared_event_open():
    a, b = make_record(), make_record(time='2020-01-01T00:00:03Z', lat=38.05)
    _, registry, _ = build.group_models([('A.json', a), ('B.json', b)], empty_registry(), {})
    key = '2020-01-01_test-earthquake'
    assert set(registry['assignments'].values()) == {key}
    # B is split off. A keeps the shared event, which still takes new models ("matches"); C's resemblance
    # to B's separate event needs a decision. The result does not depend on the registry history.
    models = [('A.json', a), ('B.json', b), ('C.json', make_record(time='2020-01-01T00:00:05Z'))]
    for start in (registry, empty_registry()):
        with pytest.raises(build.MatchError) as info:
            build.group_models(models, copy.deepcopy(start), {'B.json': 'separate'})
        [entry] = info.value.review
        assert entry['matches'] == [key] and entry['possible_matches'] == [f'{key}-7f8f16']
    _, final, _ = build.group_models(models, copy.deepcopy(registry), {'B.json': 'separate', 'C.json': key})
    assert final['assignments'] == {'A.json': key, 'B.json': f'{key}-7f8f16', 'C.json': key}


def test_one_match_plus_a_possible_match_needs_review():
    a, b = make_record(), make_record(name='Other quake', lat=38.4)       # ~44 km apart
    _, registry, _ = build.group_models([('A.json', a), ('B.json', b)], empty_registry(), {'B.json': 'separate'})
    c = make_record(lat=38.1)                                             # ~11 km from A, ~33 km from B
    with pytest.raises(build.MatchError) as info:
        build.group_models([('A.json', a), ('B.json', b), ('C.json', c)], registry, {})
    assert info.value.review == [{'file': 'C.json', 'matches': ['2020-01-01_test-earthquake'],
                                  'possible_matches': ['2020-01-01_other-quake']}]


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


def test_undated_models_get_non_blocking_review_notes(site, capsys):
    draft = make_record(name='Draft quake', time=None)
    draft['status'] = 'draft'
    site.add('A.json', make_record())
    site.add('B.json', make_record(name='Old quake', time=None))
    site.add('Draft.json', draft)                                      # drafts are not published, so not listed
    assert build.main() == 0
    out = capsys.readouterr().out
    assert site.dist.exists()
    review = json.loads(site.review.read_text(encoding='utf-8'))
    assert review['status'] == 'non_blocking_metadata_warnings'
    assert review['warnings'] == [{
        'file': 'B.json', 'reason': 'missing event.origin_time', 'event_key': 'undated_old-quake',
        'action': 'Retained as an independent provisional earthquake. Add a sourced UTC origin_time in the '
                  'contributor JSON or explicitly map the filename to a verified event key in '
                  'event_overrides.json. Never guess an event match.'}]
    assert review['instructions'].startswith('These models were published separately because their origin times')
    assert out.endswith('WARNING: 1 model(s) lack a verified UTC origin time:\n'
                        '  B.json: standalone provisional event; no automatic merging\n'
                        f'Non-blocking review notes: {site.review}\n')
    # The origin time is added: the notes are removed.
    site.add('B.json', make_record(name='Old quake', time='1990-01-01T00:00:00Z'))
    assert build.main() == 0
    assert not site.review.exists() and 'lack a verified UTC origin time' not in capsys.readouterr().out
    # An override is a maintainer's decision: no note for that file.
    site.add('B.json', make_record(name='Old quake', time=None))
    site.overrides.write_text(json.dumps({'B.json': 'separate'}))
    assert build.main() == 0
    assert not site.review.exists() and 'lack a verified UTC origin time' not in capsys.readouterr().out


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


@pytest.mark.parametrize('changes', [{}, dict(time=None), dict(lat=None, lon=None)])
def test_renamed_record_keeps_its_event_key(changes):
    """Also for undated records (never matched) and records without coordinates (matched only within 1 s)."""
    record, other = make_record(**changes), ('Other.json', make_record(name='Other quake', time='2021-01-01T00:00:00Z'))
    _, registry, _ = build.group_models([('A.json', record), other], empty_registry(), {})
    _, again, notes = build.group_models([('A_renamed.json', record), other], copy.deepcopy(registry), {})
    assert again['assignments'] == {'A_renamed.json': registry['assignments']['A.json'],
                                    'Other.json': registry['assignments']['Other.json']}
    assert again['events'] == registry['events'] and notes == []


def test_orphaned_events_are_pruned():
    models = [('A.json', make_record()), ('B.json', make_record(time='2021-01-01T00:00:00Z'))]
    _, registry, _ = build.group_models(models, empty_registry(), {})
    _, pending, _ = build.group_models(models[:1], copy.deepcopy(registry), {})
    assert pending['assignments'] == {'A.json': '2020-01-01_test-earthquake'}
    assert list(pending['events']) == ['2020-01-01_test-earthquake']


def test_registry_event_data_follow_the_records():
    undated = make_record(name='Athens quake', time=None, lat=None, lon=None)
    _, registry, _ = build.group_models([('A.json', undated)], empty_registry(), {})
    key = 'undated_athens-quake'
    assert registry['assignments'] == {'A.json': key}
    # The contributor adds the origin time: the key stays, the event data follow the record.
    dated = make_record(name='Athens quake', lat=None, lon=None)
    _, registry, notes = build.group_models([('A.json', dated)], registry, {})
    assert registry['assignments'] == {'A.json': key}
    assert registry['events'][key]['origin_time_utc'] == '2020-01-01T00:00:00Z'
    assert notes == [f"registry event '{key}' updated from A.json: origin_time_utc None -> '2020-01-01T00:00:00Z'"]
    # A second model 3 s apart is never split off silently (A has no coordinates: only 1 s matches) ...
    second = ('B.json', make_record(name='Athens earthquake (other study)', time='2020-01-01T00:00:03Z'))
    with pytest.raises(build.MatchError) as info:
        build.group_models([('A.json', dated), second], copy.deepcopy(registry), {})
    assert info.value.review == [{'file': 'B.json', 'matches': [], 'possible_matches': [key]}]
    # ... and joins automatically once both records have coordinates; the event name is the current one.
    resolved, again, _ = build.group_models([('A.json', make_record(name='Athens earthquake')), second],
                                            copy.deepcopy(registry), {})
    assert again['assignments'] == {'A.json': key, 'B.json': key}
    assert again['events'][key]['name'] == 'Athens earthquake'
    assert {m['_browser_event_name'] for _, m in resolved} == {'Athens earthquake'}


def test_registry_changes_that_need_attention_are_noted():
    models = [('A.json', make_record()), ('B.json', make_record(time='2020-01-01T00:00:03Z')),
              ('C.json', make_record(name='Other quake', lat=35.0, lon=26.0, time='2020-01-01T00:00:40Z'))]
    _, registry, _ = build.group_models(models, empty_registry(), {})
    key, other = '2020-01-01_test-earthquake', '2020-01-01_other-quake'
    assert registry['assignments'] == {'A.json': key, 'B.json': key, 'C.json': other}
    # B's date is corrected by a year: it keeps its event, with a note.
    changed = [models[0], ('B.json', make_record(time='2021-01-01T00:00:03Z')), models[2]]
    _, again, notes = build.group_models(changed, copy.deepcopy(registry), {})
    assert again['assignments'] == registry['assignments']
    assert notes == [f"B.json: kept registry event '{key}' although the record no longer matches A.json "
                     f"(31622403 s apart); add an event_overrides.json entry if this is wrong."]
    # B's magnitude is corrected to something no longer compatible: kept as well, and the note says why.
    changed = [models[0], ('B.json', make_record(time='2020-01-01T00:00:03Z', mag=7.6)), models[2]]
    _, again, notes = build.group_models(changed, copy.deepcopy(registry), {})
    assert again['assignments'] == registry['assignments']
    assert notes == [f"B.json: kept registry event '{key}' although the record no longer matches A.json "
                     f"(3 s apart, 0 km apart, magnitude 7.6 vs 6); add an event_overrides.json entry if this is wrong."]
    # C's location is corrected onto A's: both events keep their files, and the resemblance is reported.
    changed = [models[0], models[1], ('C.json', make_record(name='Other quake', time='2020-01-01T00:00:40Z'))]
    _, again, notes = build.group_models(changed, copy.deepcopy(registry), {})
    assert again['assignments'] == registry['assignments']
    assert len(notes) == 1 and notes[0].startswith(f"registry event '{other}' updated from C.json: latitude 35.0 -> 38.0")
    assert f"it now resembles ['{key}']" in notes[0]


def test_event_location_is_shared_and_enriched():
    """All models of an earthquake share one map position: the first file's hypocenter, else the first located file's."""
    models = [('A.json', make_record(name='Quake', lat=None, lon=None)),
              ('B.json', make_record(name='Quake', time='2020-01-01T00:00:01Z', lat=38.1, lon=23.1, mag=6.2)),
              ('C.json', make_record(name='Quake', time='2020-01-01T00:00:00.5Z', lat=38.05, lon=23.05)),
              ('D.json', make_record(name='Other quake', time='2021-01-01T00:00:00Z')),
              ('E.json', make_record(name='Other quake', time='2021-01-01T00:00:02Z', lat=38.1)),
              ('F.json', make_record(name='Unlocated quake', time=None, lat=None, lon=None))]
    resolved, registry, _ = build.group_models(models, empty_registry(), {})
    assert len(set(registry['assignments'].values())) == 3              # A+B+C (within 1 s, no coordinates on A), D+E, F
    located, anchor, nowhere = {'latitude': 38.1, 'longitude': 23.1}, {'latitude': 38.0, 'longitude': 23.0}, \
        {'latitude': None, 'longitude': None}
    assert {name: m['_browser_event_location'] for name, m in resolved} == {
        'A.json': located, 'B.json': located, 'C.json': located, 'D.json': anchor, 'E.json': anchor, 'F.json': nowhere}
    # Only the map position is enriched: the registry keeps the first file's own data.
    assert registry['events'][registry['assignments']['A.json']]['latitude'] is None


# ====== Contributor IDs ======


def test_omitted_ids_are_generated_in_the_published_copies(site):
    omitted = make_record()
    del omitted['model_id'], omitted['event']['event_id']
    empty = make_record(time='2021-01-01T00:00:00Z')
    empty['event']['event_id'] = ''                                   # an empty event_id counts as omitted
    site.add('A.json', omitted)
    site.add('B.json', empty)
    site.add('C.json', make_record(time='2022-01-01T00:00:00Z'))
    sources = {p: p.read_bytes() for p in build.MODELS_DIR.iterdir()}
    assert build.main() == 0
    assert {p: p.read_bytes() for p in build.MODELS_DIR.iterdir()} == sources   # never rewritten
    keys = json.loads(site.registry.read_text())['assignments']
    generated = 'auto' + hashlib.sha1(b'A.json').hexdigest()[:16] + '_tester_2021'
    assert re.fullmatch(SCHEMA['properties']['model_id']['pattern'], generated)
    public = json.loads((site.data / 'models.json').read_text(encoding='utf-8'))
    for models in (json.loads(site.blob()), public):
        assert [m['model_id'] for m in models] == [generated, 'testquake2020_tester_2021', 'testquake2020_tester_2021']
        assert [m['event']['event_id'] for m in models] == [keys['A.json'], keys['B.json'], 'testquake2020']
    assert json.loads((site.data / 'A.json').read_text(encoding='utf-8')) == public[0]


@pytest.mark.parametrize('reference, suffix', [
    ({'authors': ['A. Tester'], 'year': 2021}, '_tester_2021'),
    ({'authors': ['Müller, K.', 'B. Other'], 'year': 1999}, '_muller_1999'),
    ({'authors': ['K. de la Cruz-Smith'], 'year': 21}, '_cruzsmith_2000'),
    ({'authors': ['Παπαδόπουλος'], 'year': 2021}, '_unknown_2021'),
    ({'authors': []}, '_unknown_2000'),
])
def test_generated_model_id(reference, suffix):
    model_id = build._model_id_for('X.json', {'reference': reference})
    assert model_id == 'auto' + hashlib.sha1(b'X.json').hexdigest()[:16] + suffix
    assert re.fullmatch(SCHEMA['properties']['model_id']['pattern'], model_id)


def test_schema_ids_are_optional():
    assert 'model_id' not in SCHEMA['required'] and 'event_id' not in SCHEMA['properties']['event']['required']
    record = make_record()
    del record['model_id'], record['event']['event_id']
    assert build.schema_errors(build.Draft202012Validator(SCHEMA, format_checker=build.Draft202012Validator.FORMAT_CHECKER),
                               record) == []


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


@pytest.mark.parametrize('text, flagged', [
    ('see https://www.example.com/data', True), ('http://example.net', True),
    ('https://user@sub.example.org:8080/x', True), ('https://example.community/x', False), ('An Example of slip', False),
])
def test_placeholder_hosts(text, flagged):
    record = make_record()
    record['reference']['notes'] = text
    assert bool(build.find_placeholders(record)) == flagged


def test_proto_keys_are_rejected(site, capsys):
    record = make_record()
    record['reference']['__proto__'] = {'year': 1900}
    record['model_variant'] = {'__proto__': {'phase': 'interseismic'}}
    assert sorted(p.split(':')[0] for p in build.find_placeholders(record)) == [
        'at model_variant/__proto__', 'at reference/__proto__']
    site.add('A.json', record)
    assert build.main() == 1
    assert 'at reference/__proto__: the key "__proto__" is not allowed' in capsys.readouterr().out
    assert not site.dist.exists()


def test_invalid_unicode_is_a_validation_error(site, capsys):
    record = make_record()
    record['event']['region'] = 'Lake \ud800'
    record['reference']['\udc80key'] = 'value'
    (build.MODELS_DIR / 'A.json').write_text(json.dumps(record), encoding='utf-8')  # surrogates as \u escapes
    assert build.main() == 1
    out = capsys.readouterr().out
    assert "at event/region: text 'Lake \\ud800' is not valid Unicode" in out
    assert 'at reference/\\udc80key: key' in out
    assert not site.dist.exists() and not site.registry.exists()


@pytest.mark.parametrize('literal', ['1e999', '-1e999', '1' + '0' * 400])
def test_non_finite_numbers_are_rejected(site, capsys, literal):
    text = json.dumps(make_record(), indent=2).replace('"max_slip_m": 1.0', f'"max_slip_m": {literal}')
    (build.MODELS_DIR / 'A.json').write_text(text, encoding='utf-8')
    assert build.main() == 1
    out = capsys.readouterr().out
    assert 'INVALID JSON: A.json' in out and 'not a finite number' in out
    assert not site.dist.exists()


def test_output_is_strict_json():
    for value in (math.inf, math.nan):
        with pytest.raises(ValueError):
            build._script_json([{'x': value}])
        with pytest.raises(ValueError):
            build._json_bytes({'x': value})


def test_reserved_and_unconventional_file_names(site, capsys):
    site.add('Test2020_Tester_2021.json', make_record())
    site.add('models.json', make_record(time='2021-01-01T00:00:00Z'))
    assert build.main() == 1
    out = capsys.readouterr().out
    assert "file name 'models.json' is reserved" in out
    assert 'NOTE: file name(s) not following <EventCamelCase><Year>_<Author>_<PubYear>.json: models.json\n' in out
    assert not site.dist.exists()
    (build.MODELS_DIR / 'models.json').rename(build.MODELS_DIR / 'Other2021_Tester_2021.json')
    assert build.main() == 0
    assert 'not following' not in capsys.readouterr().out


@pytest.mark.parametrize('mutate', [
    lambda r: r['event'].update(hypocenter='TBD'),
    lambda r: r['event'].update(hypocenter=[38.0, 23.0]),
    lambda r: r.update(fault_segments=1),
    lambda r: r['fault_segments'][0].update(patches='TODO', reference_point='TBD', patch_grid=[2, 2]),
    lambda r: r['fault_segments'][0]['reference_point'].update(position=['top_left'], along_strike_km='0'),
])
def test_malformed_draft_does_not_break_the_build(site, capsys, mutate):
    draft = make_record(name='Draft quake', time='2010-01-01T00:00:00Z')
    draft['status'] = 'draft'
    mutate(draft)
    build.consistency_warnings(draft)  # the checks are type-safe on their own as well
    site.add('A.json', make_record())
    site.add('Draft.json', draft)
    assert build.main() == 0
    assert 'consistency checks skipped (record is not schema-valid)' in capsys.readouterr().out
    assert not (site.data / 'Draft.json').exists()


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
                     'implies Mw'):
        assert expected in joined
    assert 'license' not in joined  # reported once per build, not per model


def test_missing_license_is_one_build_note(site, capsys):
    unlicensed = make_record(time='2021-01-01T00:00:00Z')
    unlicensed['provenance']['license'] = None
    site.add('A.json', make_record())
    site.add('B.json', unlicensed)
    assert build.main() == 0
    assert 'NOTE: no license stated (provenance.license) in 1 published record(s): B.json' in capsys.readouterr().out
    assert [m['_browser_warnings'] for m in json.loads(site.blob())] == [[], []]


def test_record_without_any_coordinates_is_not_shown_on_the_map():
    warning = 'no hypocenter, reference-point or patch coordinates: not shown on the map'

    def unanchored():
        record = make_record(lat=None, lon=None)
        for patch in record['fault_segments'][0].get('patches') or []:
            patch.update(latitude=None, longitude=None)
        return record

    assert warning in build.consistency_warnings(unanchored())
    record = unanchored()
    record['fault_segments'][0]['reference_point'].update(latitude=38.0, longitude=23.0)
    assert warning not in build.consistency_warnings(record)
    record = unanchored()
    patches = record['fault_segments'][0].get('patches')
    assert patches
    patches[0].update(latitude=38.0, longitude=23.0)
    assert warning not in build.consistency_warnings(record)


@pytest.mark.parametrize('position, along, down, warned', [
    ('top_left', 0.0, 0.0, False), ('top_left', 1.0, 0.0, True),
    ('top_center', 1.0, 0.0, False), ('top_center', 1.0, 1.0, True),
    ('center', 1.0, 1.0, False), ('center', 1.01, 0.99, False), ('center', 1.0, 0.0, True),
    ('hypocenter', 0.3, 1.7, False), ('unknown', 2.0, 2.0, False),
])
def test_reference_point_offsets_must_agree_with_position(position, along, down, warned):
    record = make_record()  # 2 km × 2 km plane: tolerance 0.02 km
    record['fault_segments'][0]['reference_point'].update(position=position, along_strike_km=along, down_dip_km=down)
    warnings = build.consistency_warnings(record)
    assert any(w.startswith('seg1: reference_point position') for w in warnings) == warned


def test_reference_point_offsets_come_in_pairs(site, capsys):
    record = make_record()
    record['fault_segments'][0]['reference_point']['down_dip_km'] = 1.0
    site.add('A.json', record)
    assert build.main() == 1
    assert ("at fault_segments/0/reference_point: 'along_strike_km' is a dependency of 'down_dip_km'"
            in capsys.readouterr().out)


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
    assert [m['_browser_event_location'] for m in models] == [{'latitude': 38.0, 'longitude': 23.0}] * 2
    assert {m['_browser_source_file'] for m in models} == {'A.json', 'B.json'}
    public = json.loads((site.data / 'models.json').read_text())
    assert len(public) == 2 and not any(k.startswith('_browser_') for m in public for k in m)
    assert sorted(p.name for p in site.data.iterdir()) == ['A.json', 'B.json', 'models.json']
    assert json.loads((site.data / 'A.json').read_text()) == public[0]


def test_output_is_rendered_before_anything_is_written(site, monkeypatch, capsys):
    site.add('A.json', make_record())
    assert build.main() == 0
    before = {p: p.read_bytes() for p in (site.dist, site.registry, *site.data.iterdir())}
    site.add('B.json', make_record(time='2021-01-01T00:00:00Z'))
    group_models = build.group_models

    def unwritable(*args):  # the last record's page data cannot be written as JSON
        resolved, pending, notes = group_models(*args)
        resolved[-1][1]['_browser_event_name'] = math.inf
        return resolved, pending, notes

    monkeypatch.setattr(build, 'group_models', unwritable)
    assert build.main() == 1
    assert 'BUILD FAILED preparing output' in capsys.readouterr().out
    assert {p: p.read_bytes() for p in before} == before
    assert sorted(p.name for p in site.dist.parent.rglob('*')) == ['A.json', 'data', 'index.html', 'models.json']


def test_real_repository_builds(tmp_path, monkeypatch, capsys):
    """Smoke test: the committed data, schema and template build cleanly (outputs go to tmp)."""
    for name in ('event_registry.json', 'event_overrides.json'):
        if (ROOT / name).exists():
            shutil.copy(ROOT / name, tmp_path / name)
    monkeypatch.setattr(build, 'DIST_PATH', tmp_path / 'dist' / 'index.html')
    monkeypatch.setattr(build, 'REGISTRY_PATH', tmp_path / 'event_registry.json')
    monkeypatch.setattr(build, 'OVERRIDES_PATH', tmp_path / 'event_overrides.json')
    monkeypatch.setattr(build, 'REVIEW_PATH', tmp_path / 'event_review.json')
    page_data = []
    script_json = build._script_json
    monkeypatch.setattr(build, '_script_json', lambda models: page_data.append(models) or script_json(models))
    assert build.main() == 0, capsys.readouterr().out
    files = sorted((ROOT / 'data' / 'models').glob('*.json'))
    published = [f for f in files if json.loads(f.read_text(encoding='utf-8-sig')).get('status') != 'draft']
    public = json.loads((tmp_path / 'dist' / 'data' / 'models.json').read_text(encoding='utf-8'))
    assert len(public) == len(published) > 0
    assert all(m.get('model_id') and m['event'].get('event_id') for m in public)
    locations = {}
    for m in page_data[-1]:
        assert set(m['_browser_event_location']) == {'latitude', 'longitude'}
        assert locations.setdefault(m['_browser_event_key'], m['_browser_event_location']) == m['_browser_event_location']
    html = (tmp_path / 'dist' / 'index.html').read_text(encoding='utf-8')
    assert '__MODELS_JSON__' not in html and '__MODEL_COUNT__' not in html
