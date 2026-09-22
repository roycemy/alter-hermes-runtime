from safety import inspect

def test_blocks_school_submission(): assert not inspect("submit my graded Canvas assignment").allowed
def test_blocks_money(): assert not inspect("buy a domain").allowed
def test_allows_build_draft(): assert inspect("build a local landing page draft").allowed
