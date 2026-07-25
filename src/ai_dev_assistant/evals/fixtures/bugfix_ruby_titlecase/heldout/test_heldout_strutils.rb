# Held-out tests (E3): the agent NEVER sees this file — it lives outside the fixture
# repo and is merged into the finished workspace by the ``heldout_tests_pass`` grader,
# where `rake test` (pattern test_*.rb) runs it alongside the in-repo tests.
require "minitest/autorun"
require "strutils"

class TestHeldoutStrUtils < Minitest::Test
  def test_every_word_capitalized
    assert_equal "A Fine Day Indeed", StrUtils.titlecase("a fine day indeed")
    assert_equal "One Two", StrUtils.titlecase("ONE TWO")
  end

  def test_whitespace_handling
    assert_equal "Tabs And Newlines", StrUtils.titlecase("\ttabs\n and\r\n newlines ")
  end

  def test_single_word_and_empty_unbroken
    assert_equal "Word", StrUtils.titlecase("WORD")
    assert_equal "", StrUtils.titlecase("")
  end

  def test_word_count_unbroken
    assert_equal 4, StrUtils.word_count(" a  b\tc \nd ")
  end
end
