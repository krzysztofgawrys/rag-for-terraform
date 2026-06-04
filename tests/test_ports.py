"""Tests for deterministic port resolution (app/core/ports.py).

The whole point is that ports come from authoritative data (the canonical rules
map or inline literals), never from a name heuristic or an LLM - so these are
pure, exact assertions.
"""
from app.core.ports import build_rule_port_map, resolve_ports, extract_query_ports

# Shape of the real terraform-aws-security-group `rules` map.
RULES = {
    "redis-tcp": [6379, 6379, "tcp", "Redis"],
    "mysql-tcp": [3306, 3306, "tcp", "MySQL/Aurora"],
    "ssh-tcp": [22, 22, "tcp", "SSH"],
    "solr-tcp": [8983, 8987, "tcp", "Solr"],         # small range
    "dns-udp": [53, 53, "udp", "DNS"],
    "all-all": ["${-1}", "${-1}", "-1", "All protocols"],  # sentinel, skip
    "all-tcp": [0, 65535, "tcp", "All TCP ports"],         # wildcard span, skip
}


def test_build_map_specific_ports():
    m = build_rule_port_map(RULES)
    assert m["redis-tcp"] == [6379]
    assert m["mysql-tcp"] == [3306]
    assert m["dns-udp"] == [53]


def test_build_map_expands_small_range():
    assert build_rule_port_map(RULES)["solr-tcp"] == [8983, 8984, 8985, 8986, 8987]


def test_build_map_skips_wildcards_and_sentinels():
    m = build_rule_port_map(RULES)
    assert "all-all" not in m   # "${-1}" is not an int
    assert "all-tcp" not in m   # 0..65535 span exceeds the range cap


def test_resolve_named_rule():
    m = build_rule_port_map(RULES)
    assert resolve_ports({"auto_ingress_rules": {"default": ["redis-tcp"]}}, "", m) == [6379]


def test_resolve_multiple_dedup_sorted():
    m = build_rule_port_map(RULES)
    variables = {
        "auto_ingress_rules": {"default": ["redis-tcp", "ssh-tcp"]},
        "ingress_rules": {"default": ["redis-tcp"]},   # duplicate
    }
    assert resolve_ports(variables, "", m) == [22, 6379]


def test_resolve_unknown_rule_ignored():
    m = build_rule_port_map(RULES)
    assert resolve_ports({"auto_ingress_rules": {"default": ["no-such-rule"]}}, "", m) == []


def test_resolve_inline_from_port_literal():
    code = 'ingress {\n  from_port = 8200\n  to_port = 8200\n}'
    assert resolve_ports({}, code, {}) == [8200]


def test_resolve_combines_named_and_inline():
    m = build_rule_port_map(RULES)
    variables = {"auto_ingress_rules": {"default": ["redis-tcp"]}}
    assert resolve_ports(variables, "from_port = 9121", m) == [6379, 9121]


def test_extract_query_ports_4_5_digit_standalone():
    assert extract_query_ports("an in-memory cache on 6379") == [6379]
    assert extract_query_ports("event broker on 9092 and 9094") == [9092, 9094]


def test_extract_query_ports_short_needs_port_keyword():
    assert extract_query_ports("open port 443 for https") == [443]
    assert extract_query_ports("port 22 for ssh") == [22]


def test_extract_query_ports_no_false_positives():
    assert extract_query_ports("deploy 3 subnets with a 24-bit mask") == []
    assert extract_query_ports("") == []
    assert extract_query_ports(None) == []
