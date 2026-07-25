# Tiny string helpers (golden-task eval fixture).
module StrUtils
  module_function

  # Title-case a sentence: every word capitalized, whitespace collapsed to
  # single spaces, surrounding whitespace stripped.
  def titlecase(s)
    words = s.to_s.strip.split(/\s+/)
    # BUG: only the first word is capitalized; the rest are merely downcased,
    # so titlecase("hello brave world") returns "Hello brave world".
    words.each_with_index.map { |w, i| i.zero? ? w.capitalize : w.downcase }.join(" ")
  end

  # Number of whitespace-separated words in the string.
  def word_count(s)
    s.to_s.strip.split(/\s+/).reject(&:empty?).length
  end
end
