-- Cleanup of mis-indexed module versions caused by a bug in
-- _match_tag_to_modules (app/services/indexer.py).
--
-- Bug: when a git tag had a module-name prefix (e.g. 'vpc_peering-1.0.0')
-- but no module in HEAD matched that prefix, the function returned None
-- instead of [], which caused run_tag_indexing to fall back to "index ALL
-- modules in the repo" — applying that unrelated tag to every module.
--
-- Symptom in DB: a single tag (e.g. 'vpc_peering-1.0.0') appears as the
-- 'version' for 60+ modules across the repo. Reported in get_module_details
-- as "Latest version" and surfaced in MCP query_modules as Source ?ref=...
--
-- Heuristic: a version row is junk when
--   1. version has the form <prefix>-X.Y... or <prefix>_X.Y... (also -vX.Y)
--   2. <prefix> contains at least one letter (excludes pure '1.0.2' tags)
--   3. <prefix> (with _ → -) is NOT a substring of the module_path
--      (with _ and / → -), i.e. the tag does not belong to this module.
--
-- Pure version tags ('v1.40', '1.0.2'), branch refs ('master', 'main') and
-- correctly matched module tags ('artemis-2.0.1' on managed/artemis) are
-- left intact. Expected delete: ~786 rows.

DELETE FROM modules
WHERE version ~ '[-_]v?[0-9]+\.[0-9]'
  AND regexp_replace(version, '[-_]v?[0-9]+\.[0-9].*$', '') ~ '[a-zA-Z]'
  AND LOWER(translate(module_path, '_/', '--')) NOT LIKE
      '%' || LOWER(replace(
          regexp_replace(version, '[-_]v?[0-9]+\.[0-9].*$', ''),
          '_', '-'
      )) || '%';
