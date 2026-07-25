// In-repo tests. These FAIL at baseline (IsLeapYear ignores the century rules);
// the agent must fix leapyear.go — not these tests — to make `go test` pass.
package leapyear

import "testing"

func TestIsLeapYearDivisibleByFour(t *testing.T) {
	if !IsLeapYear(2024) {
		t.Fatalf("2024 should be a leap year")
	}
	if IsLeapYear(2023) {
		t.Fatalf("2023 should not be a leap year")
	}
}

func TestIsLeapYearCenturyRules(t *testing.T) {
	if IsLeapYear(1900) {
		t.Fatalf("1900 is divisible by 100 but not 400: not a leap year")
	}
	if !IsLeapYear(2000) {
		t.Fatalf("2000 is divisible by 400: a leap year")
	}
}

func TestDaysInFebruary(t *testing.T) {
	if got := DaysInFebruary(2024); got != 29 {
		t.Fatalf("Feb 2024: want 29, got %d", got)
	}
	if got := DaysInFebruary(1900); got != 28 {
		t.Fatalf("Feb 1900: want 28, got %d", got)
	}
}
