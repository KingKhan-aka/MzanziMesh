from pathlib import Path

from sentinel_ops.roles_api import roles


def test_role_contract_matches_required_workspaces():
    payload = roles()
    by_id = {item["id"]: item for item in payload["roles"]}

    assert by_id["member"]["home"] == "property"
    assert "own cameras" in by_id["member"]["sees"]
    assert "individual claim files" in by_id["member"]["never"]

    assert by_id["fraud"]["home"] == "claims"
    assert "all claims" in by_id["fraud"]["sees"]
    assert "member camera control" in by_id["fraud"]["never"]

    assert by_id["security"]["home"] == "dispatch"
    assert "patrol routes" in by_id["security"]["sees"]
    assert "claim amounts" in by_id["security"]["never"]

    assert "demo role selector" in payload["principle"]
    assert "authenticated identities" in payload["principle"]


def test_dashboard_has_strict_role_tab_sets_and_role_chooser():
    html_path = Path(__file__).resolve().parents[1] / "static" / "dashboard.html"
    html = html_path.read_text(encoding="utf-8")

    assert 'id="roleGate"' in html
    assert 'data-role-choice="member"' in html
    assert 'data-role-choice="fraud"' in html
    assert 'data-role-choice="security"' in html

    assert "member:['property','live']" in html
    # Abed's finals frontend deliberately consolidates fraud review into one
    # claim-centred workspace; evidence, movement, rewind and patterns are tools
    # inside that selected case instead of separate top-level navigation tabs.
    assert "fraud:['claims']" in html
    # Security follows the same finals pattern: one response desk with patrol
    # planning and briefing available contextually inside the workspace.
    assert "security:['dispatch']" in html

    assert "showRoleGate();\n  loadAI();" in html
    assert "applyRole('fraud');" not in html

    assert 'id="mContribution"' in html
    assert 'id="mPoints"' in html
    assert "no live Vitality API is connected" in html


def test_finals_workspace_order_and_map_layers_are_visible():
    html_path = Path(__file__).resolve().parents[1] / "static" / "dashboard.html"
    html = html_path.read_text(encoding="utf-8")

    # The patrol proof belongs inside the single security response desk, and the
    # technical consoles are deliberately the final section of the live demo.
    planning = html.index('class="panel security-planning-panel"')
    patrol = html.index('id="securityPatrolComparison"')
    consoles = html.index('id="secConsoleGrid"')
    assert planning < patrol < consoles

    # Mapbox Standard needs custom operational layers in its top slot; otherwise
    # the three per-house markers can exist in GeoJSON but render below the map.
    assert "function memberMapAddOperationalLayer" in html
    assert "if(memberMapProvider==='mapbox')layer.slot='top'" in html
    assert "memberMapAddOperationalLayer(map,{id:'mesh-camera-points'" in html


def test_claims_demo_uses_supplied_rows_and_prepared_real_clips():
    html_path = Path(__file__).resolve().parents[1] / "static" / "dashboard.html"
    html = html_path.read_text(encoding="utf-8")

    assert 'id="openEvidenceDemoClaim"' in html
    assert "showPreparedCameraInbox(d)" in html
    assert "Real workbook claim + 2 consented vehicle clips" in html
