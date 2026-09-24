import importlib.util, sys
spec = importlib.util.spec_from_file_location("chk", "tests/unit/test_no_verbatim_previous_line_dup.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
non_test, test = m._derived_sites()
listed = {(f, s): c for f, s, c in m.TEST_INTENTIONAL_REPEATS}
print("derived_non_test_sites", len(non_test))
print("derived_test_pairs", len(test), "derived_test_sites_sum", sum(test.values()))
print("listed_entries", len(listed), "listed_count_sum", sum(listed.values()))
print("derived_equals_listed_with_counts", dict(test) == listed)
for (f, s), c in sorted(listed.items()):
    print("LISTED", c, f, s)
