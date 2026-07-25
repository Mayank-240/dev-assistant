// Package leapyear is a tiny calendar helper (golden-task eval fixture).
package leapyear

// IsLeapYear reports whether year is a leap year in the Gregorian calendar.
//
// BUG: the century rules are missing — years divisible by 100 are NOT leap
// years unless they are also divisible by 400 (1900 is not a leap year,
// 2000 is).
func IsLeapYear(year int) bool {
	return year%4 == 0
}

// DaysInFebruary returns the number of days in February for the given year.
func DaysInFebruary(year int) int {
	if IsLeapYear(year) {
		return 29
	}
	return 28
}
