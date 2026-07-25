//! Tiny temperature-conversion crate (golden-task eval fixture).

/// Convert degrees Celsius to degrees Fahrenheit.
pub fn celsius_to_fahrenheit(c: f64) -> f64 {
    c * 9.0 / 5.0 + 32.0
}

/// Convert degrees Fahrenheit to degrees Celsius.
///
/// BUG: the conversion factor is inverted — it multiplies by 9/5 instead of
/// 5/9, so e.g. 212 °F comes back as 324 °C instead of 100 °C.
pub fn fahrenheit_to_celsius(f: f64) -> f64 {
    (f - 32.0) * 9.0 / 5.0
}

#[cfg(test)]
mod tests {
    // In-repo tests. These FAIL at baseline (fahrenheit_to_celsius is wrong);
    // the fix belongs in the function above, NOT in these tests.
    use super::*;

    fn close(a: f64, b: f64) -> bool {
        (a - b).abs() < 1e-9
    }

    #[test]
    fn c_to_f() {
        assert!(close(celsius_to_fahrenheit(0.0), 32.0));
        assert!(close(celsius_to_fahrenheit(100.0), 212.0));
    }

    #[test]
    fn f_to_c() {
        assert!(close(fahrenheit_to_celsius(32.0), 0.0));
        assert!(close(fahrenheit_to_celsius(212.0), 100.0));
    }

    #[test]
    fn round_trip() {
        assert!(close(fahrenheit_to_celsius(celsius_to_fahrenheit(37.0)), 37.0));
    }
}
