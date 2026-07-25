# In-repo tests. These FAIL at baseline (titlecase only capitalizes the first word);
# the agent must fix strutils.rb — not these tests — to make `rake test` pass.
require "minitest/autorun"
require "strutils"

class TestStrUtils < Minitest::Test
  def test_titlecase_capitalizes_every_word
    assert_equal "Hello Brave World", StrUtils.titlecase("hello brave world")
  end

  def test_titlecase_normalizes_case_and_whitespace
    assert_equal "The Quick Fox", StrUtils.titlecase("  the   QUICK fox ")
  end

  def test_titlecase_single_word_and_empty
    assert_equal "Ruby", StrUtils.titlecase("ruby")
    assert_equal "", StrUtils.titlecase("   ")
  end

  def test_word_count
    assert_equal 3, StrUtils.word_count("one two three")
    assert_equal 0, StrUtils.word_count("")
  end
end
