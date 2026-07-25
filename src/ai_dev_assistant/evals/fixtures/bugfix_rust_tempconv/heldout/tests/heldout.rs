//! Held-out integration tests (E3): the agent NEVER sees this file — it lives outside
//! the fixture repo and is merged into the finished workspace (as tests/heldout.rs) by
//! the ``heldout_tests_pass`` grader, where `cargo test` runs it.

use tempconv::{celsius_to_fahrenheit, fahrenheit_to_celsius};

fn close(a: f64, b: f64) -> bool {
    (a - b).abs() < 1e-9
}

#[test]
fn heldout_freezing_and_boiling() {
    assert!(close(fahrenheit_to_celsius(32.0), 0.0));
    assert!(close(fahrenheit_to_celsius(212.0), 100.0));
}

#[test]
fn heldout_negative_and_crossover() {
    assert!(close(fahrenheit_to_celsius(-40.0), -40.0));
    assert!(close(celsius_to_fahrenheit(-40.0), -40.0));
}

#[test]
fn heldout_round_trips() {
    for c in [-30.0_f64, 0.0, 16.5, 100.0] {
        assert!(close(fahrenheit_to_celsius(celsius_to_fahrenheit(c)), c));
    }
}
