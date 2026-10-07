# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: ``session.id`` is recovered from any W3C ``baggage`` header.

For any uuid4 string S and any list of other baggage members (arbitrary
keys/values, optional ``;property`` suffixes), placing ``session.id=S`` at
any position in the comma-separated member list and parsing the resulting
``baggage`` header SHALL return exactly S.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from container.code_mcp_server import _runtime_session_id_from_headers

# Baggage keys/values are token / percent-encoded strings: no ',', ';', '=',
# or whitespace. Keep them printable ASCII and never equal to 'session.id'.
_token_chars = st.sampled_from(
    list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.!#$%&'*+^`|~")
)
_key_st = st.text(alphabet=_token_chars, min_size=1, max_size=20).filter(
    lambda k: k != "session.id"
)
_value_st = st.text(alphabet=_token_chars, min_size=0, max_size=40)
_prop_st = st.lists(
    st.text(alphabet=_token_chars, min_size=1, max_size=12), min_size=0, max_size=2
)


@st.composite
def other_member_st(draw: st.DrawFn) -> str:
    key = draw(_key_st)
    value = draw(_value_st)
    props = draw(_prop_st)
    member = f"{key}={value}"
    for p in props:
        member += f";{p}"
    return member


@st.composite
def baggage_with_session_st(draw: st.DrawFn) -> tuple[str, str]:
    """Return (header_value, session_id) with session.id at a random position."""
    sid = str(draw(st.uuids(version=4)))
    others = draw(st.lists(other_member_st(), min_size=0, max_size=6))
    session_member = f"session.id={sid}"
    if draw(st.booleans()):
        session_member += ";" + ";".join(draw(_prop_st) or ["p"])
    position = draw(st.integers(min_value=0, max_value=len(others)))
    members = others[:position] + [session_member] + others[position:]
    # Optional surrounding whitespace around each member (OWS is allowed).
    pad = draw(st.sampled_from(["", " ", "  "]))
    header = ",".join(f"{pad}{m}{pad}" for m in members)
    return header, sid


@given(data=baggage_with_session_st())
@settings(max_examples=200)
def test_session_id_recovered_from_any_member_order(data):
    header, sid = data
    assert _runtime_session_id_from_headers({"baggage": header}) == sid


@given(data=baggage_with_session_st())
@settings(max_examples=100)
def test_header_name_case_is_irrelevant(data):
    header, sid = data
    assert _runtime_session_id_from_headers({"Baggage": header}) == sid
    assert _runtime_session_id_from_headers({"BAGGAGE": header}) == sid


@given(others=st.lists(other_member_st(), min_size=0, max_size=6))
@settings(max_examples=100)
def test_no_session_member_returns_empty(others):
    header = ",".join(others)
    assert _runtime_session_id_from_headers({"baggage": header}) == ""
