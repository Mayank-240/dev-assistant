// Held-out tests (E3): the agent NEVER sees this file — it lives outside the fixture
// repo and is merged into the finished workspace by the ``heldout_tests_pass`` grader,
// where `go test ./...` runs it alongside the in-repo tests (same package).
package leapyear

import "testing"

func TestHeldoutCenturyYears(t *testing.T) {
	cases := map[int]bool{
		1600: true,
		1700: false,
		1800: false,
		1900: false,
		2000: true,
		2100: false,
	}
	for year, want := range cases {
		if got := IsLeapYear(year); got != want {
			t.Errorf("IsLeapYear(%d) = %v, want %v", year, got, want)
		}
	}
}

func TestHeldoutOrdinaryYears(t *testing.T) {
	if !IsLeapYear(1996) {
		t.Errorf("1996 should be a leap year")
	}
	if IsLeapYear(2019) {
		t.Errorf("2019 should not be a leap year")
	}
}

func TestHeldoutDaysInFebruary(t *testing.T) {
	if got := DaysInFebruary(2000); got != 29 {
		t.Errorf("Feb 2000: want 29, got %d", got)
	}
	if got := DaysInFebruary(2100); got != 28 {
		t.Errorf("Feb 2100: want 28, got %d", got)
	}
}
