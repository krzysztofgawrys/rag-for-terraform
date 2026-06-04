"""Deterministic listening-port resolution for modules.

The port a module's security group opens is resolved ONLY from authoritative
sources - never guessed from a rule/service name and never via an LLM:

  1. Named rules: a preset references rules by name (`auto_ingress_rules =
     ["redis-tcp"]`). The canonical `rules` map (the security-group root
     module's `rules` variable default: name -> [from_port, to_port, protocol,
     description]) resolves the name to its real port.
  2. Inline literals: a module may declare `from_port = 6379` directly.

Both are facts in the indexed data. Wildcard rules (all-all, all-tcp, ...) carry
no specific port and are skipped - they are not service discriminators.
"""
import re

_PROTO_OK = {"tcp", "udp"}
_RANGE_MAX = 16  # expand small port ranges; a wider span (all-tcp 0-65535) is a wildcard
_INGRESS_RULE_VARS = ("auto_ingress_rules", "ingress_rules", "computed_ingress_rules")
_FROM_PORT_RE = re.compile(r"from_port\s*=\s*(\d{1,5})")


def build_rule_port_map(rules_default: dict | None) -> dict[str, list[int]]:
    """Canonical rule-name -> listening ports, from the `rules` variable default.

    Each entry is [from_port, to_port, protocol, description]. Non-numeric or
    out-of-range ports (e.g. the "${-1}" all-protocols sentinel) and spans wider
    than _RANGE_MAX (wildcards) are skipped. Small ranges are expanded so a query
    for any port in the range matches.
    """
    out: dict[str, list[int]] = {}
    for name, spec in (rules_default or {}).items():
        if not isinstance(spec, (list, tuple)) or len(spec) < 3:
            continue
        fp, tp, proto = spec[0], spec[1], spec[2]
        if proto not in _PROTO_OK:
            continue
        if not isinstance(fp, int) or not isinstance(tp, int):
            continue
        if fp < 1 or fp > 65535 or tp < fp or tp > 65535 or tp - fp > _RANGE_MAX:
            continue
        out[name] = list(range(fp, tp + 1))
    return out


def _rule_names(variables: dict | None) -> list[str]:
    """Ingress rule names a module references, from its rule-list variable defaults."""
    names: list[str] = []
    for var in _INGRESS_RULE_VARS:
        spec = (variables or {}).get(var)
        default = spec.get("default") if isinstance(spec, dict) else None
        if isinstance(default, list):
            names.extend(n for n in default if isinstance(n, str))
    return names


def resolve_ports(variables: dict | None, raw_code: str | None,
                  rule_map: dict[str, list[int]]) -> list[int]:
    """Sorted, de-duplicated listening ports for a module.

    Named ingress rules resolved through `rule_map`, plus inline `from_port = N`
    literals from the code. Deterministic - never infers from names or an LLM.
    """
    ports: set[int] = set()
    for name in _rule_names(variables):
        ports.update(rule_map.get(name, ()))
    for m in _FROM_PORT_RE.finditer(raw_code or ""):
        p = int(m.group(1))
        if 1 <= p <= 65535:
            ports.add(p)
    return sorted(ports)


_QUERY_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def query_tokens(text: str | None) -> set[str]:
    """Lowercase alphanumeric tokens (length >= 3) in a string.

    Used by the lexical-fusion gate: a query token that matches a catalog
    service-name token is a discriminator (so lexical helps); a pure category
    paraphrase has no such token (so lexical only adds noise).
    """
    return set(_QUERY_TOKEN_RE.findall((text or "").lower()))


_QUERY_PORT_RE = re.compile(r"(port\s+)?(\d{2,5})", re.IGNORECASE)


def extract_query_ports(query: str | None) -> list[int]:
    """Port numbers a query refers to, conservatively.

    A number qualifies only if it is 4-5 digits (almost always a port in this
    domain, e.g. 6379, 9092) OR is explicitly preceded by "port" (catches the
    shorter well-known ports like "port 443"). This avoids boosting on incidental
    small counts ("3 subnets", "24-bit mask").
    """
    out: list[int] = []
    for prefix, digits in _QUERY_PORT_RE.findall(query or ""):
        n = int(digits)
        if 1 <= n <= 65535 and (prefix or len(digits) >= 4) and n not in out:
            out.append(n)
    return out
