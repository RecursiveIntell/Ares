// Qualification only: calls the real Libraries profile-runtime owner.
#![allow(clippy::expect_used)]

use profile_runtime::{
    compose_profile_runtime, resolve_policy_basis, ApplicabilityContextV1,
    CompiledObligationKindV1, CompositionRuleSetV1, FoldClassV1, ObligationContributionV1,
    PolicyBasisTaskContextV1, ProfileRefGroupV1, ProfileSetV1,
};
use stack_ids::ResidencyPolicyProfileId;

fn context() -> ApplicabilityContextV1 {
    ApplicabilityContextV1::new(
        "mission-space",
        None,
        "2026-09-05T00:00:00Z",
        "2026-09-05T20:00:01Z",
        "sealed_completion",
        "principal",
        vec!["role:principal".into()],
        None,
        None,
        Some("normal".into()),
    )
}

fn profile_set(context: &ApplicabilityContextV1) -> ProfileSetV1 {
    ProfileSetV1::new(
        context.applicability_context_id.clone(),
        ProfileRefGroupV1 {
            residency_policy_profile_id: Some(ResidencyPolicyProfileId::new("rpp_policy")),
            ..ProfileRefGroupV1::default()
        },
        vec![
            "authority:policy-owner:7".into(),
            "source:profile-runtime:5".into(),
        ],
    )
}

fn strings(
    family: &str,
    key: &str,
    kind: CompiledObligationKindV1,
    fold: FoldClassV1,
    values: &[&str],
) -> ObligationContributionV1 {
    ObligationContributionV1 {
        obligation_family: family.into(),
        obligation_key: key.into(),
        output_kind: kind,
        fold_class: fold,
        string_values: values.iter().map(|value| (*value).into()).collect(),
        numeric_value: None,
        expiry_at: None,
        blocking: false,
        source_profile_ref: "rpp_policy".into(),
        admissible_exception_classes: Vec::new(),
        explanation: format!("fixture {family}"),
    }
}

fn number(family: &str, key: &str, value: i64) -> ObligationContributionV1 {
    ObligationContributionV1 {
        obligation_family: family.into(),
        obligation_key: key.into(),
        output_kind: CompiledObligationKindV1::Effect,
        fold_class: FoldClassV1::MinOfMaxima,
        string_values: Vec::new(),
        numeric_value: Some(value),
        expiry_at: None,
        blocking: false,
        source_profile_ref: "rpp_policy".into(),
        admissible_exception_classes: Vec::new(),
        explanation: format!("fixture {family}"),
    }
}

fn contributions() -> Vec<ObligationContributionV1> {
    vec![
        strings(
            "egress.allowed_route_classes",
            "route",
            CompiledObligationKindV1::Effect,
            FoldClassV1::Intersection,
            &["local", "managed_local"],
        ),
        strings(
            "egress.allowed_route_classes",
            "route",
            CompiledObligationKindV1::Effect,
            FoldClassV1::Intersection,
            &["managed_local"],
        ),
        strings(
            "disclosure.allowed_classes",
            "classification",
            CompiledObligationKindV1::Disclosure,
            FoldClassV1::Intersection,
            &["public", "private"],
        ),
        strings(
            "disclosure.allowed_classes",
            "classification",
            CompiledObligationKindV1::Disclosure,
            FoldClassV1::Intersection,
            &["public"],
        ),
        strings(
            "effect.allowed_classes",
            "effect",
            CompiledObligationKindV1::Effect,
            FoldClassV1::Intersection,
            &["sealed_completion"],
        ),
        strings(
            "effect.required_preflight_checks",
            "checks",
            CompiledObligationKindV1::Check,
            FoldClassV1::Union,
            &["policy_current", "graph_obligation_current"],
        ),
        number("budget.max_input_tokens", "input", 4096),
        number("budget.max_input_tokens", "input", 2048),
        number("budget.max_output_tokens", "output", 512),
        number("budget.max_attempts", "attempts", 2),
        number("budget.max_concurrency", "concurrency", 1),
        number("budget.max_wall_time_ms", "wall_time", 30_000),
        number("budget.max_artifact_bytes", "artifact_bytes", 65_536),
        ObligationContributionV1 {
            obligation_family: "policy.not_after".into(),
            obligation_key: "expiry".into(),
            output_kind: CompiledObligationKindV1::Continuity,
            fold_class: FoldClassV1::EarliestExpiry,
            string_values: Vec::new(),
            numeric_value: None,
            expiry_at: Some("2030-01-01T00:00:00Z".into()),
            blocking: false,
            source_profile_ref: "rpp_policy".into(),
            admissible_exception_classes: Vec::new(),
            explanation: "fixture expiry".into(),
        },
    ]
}

fn task_context() -> PolicyBasisTaskContextV1 {
    PolicyBasisTaskContextV1 {
        mission_ref: "mission:1".into(),
        task_ref: "task:1".into(),
        instruction_ref: "instruction:7".into(),
        instruction_digest:
            "sha256:97985222455c141849220d0a2abc0c7e40a6057312abd92274c5af9e2908360c".into(),
        source_revision: "source:1".into(),
        authority_snapshot_ref: "authority:snapshot:7".into(),
        unresolved_instruction_obligations: vec!["operator_clause:preserve_live_state".into()],
    }
}

fn main() {
    let context = context();
    let profiles = profile_set(&context);
    let rules = CompositionRuleSetV1::reference_v1();
    let outcome = compose_profile_runtime(
        &context,
        &profiles,
        &rules,
        &contributions(),
        &[],
        "2026-09-05T20:00:02Z",
    )
    .expect("canonical owner composition");
    let v1 = resolve_policy_basis(&context, &profiles, &rules, &outcome, task_context())
        .expect("canonical owner V1");
    let first =
        profile_runtime::ResolvedPolicyBasisV2::from_v1(v1.clone()).expect("canonical owner V2");
    let mut second_task = task_context();
    second_task.task_ref = "task:2".into();
    let second = profile_runtime::resolve_policy_basis_v2(
        &context,
        &profiles,
        &rules,
        &outcome,
        second_task,
    )
    .expect("second canonical owner task");
    let mut temporal = serde_json::Map::new();
    for (name, start, end) in [
        ("z", "2026-09-05T00:00:00Z", "2030-01-01T00:00:00Z"),
        (
            "zero_offset",
            "2026-09-05T00:00:00+00:00",
            "2030-01-01T00:00:00+00:00",
        ),
        (
            "positive_offset",
            "2026-09-05T05:30:00+05:30",
            "2030-01-01T05:30:00+05:30",
        ),
        (
            "negative_offset",
            "2026-09-04T17:00:00-07:00",
            "2029-12-31T17:00:00-07:00",
        ),
        (
            "expired",
            "2026-09-04T00:00:00Z",
            "2026-09-05T05:30:00+05:30",
        ),
        (
            "future_nanosecond",
            "2026-09-05T00:00:00.000000001Z",
            "2030-01-01T00:00:00Z",
        ),
        (
            "expires_nanosecond",
            "2026-09-04T00:00:00Z",
            "2026-09-05T00:00:00.000000001Z",
        ),
        ("lowercase", "2026-09-05t00:00:00z", "2030-01-01t00:00:00z"),
        ("space", "2026-09-05 00:00:00Z", "2030-01-01 00:00:00Z"),
        (
            "long_fraction",
            "2026-09-05T00:00:00.0000000009Z",
            "2030-01-01T00:00:00Z",
        ),
        (
            "unicode_minus",
            "2026-09-04T17:00:00−07:00",
            "2029-12-31T17:00:00−07:00",
        ),
        (
            "year_zero",
            "0000-02-29T00:00:00+23:59",
            "2030-01-01T00:00:00Z",
        ),
        (
            "leap_second",
            "2016-12-31T23:59:60Z",
            "2030-01-01T00:00:00Z",
        ),
        ("inverted", "2031-01-01T00:00:00Z", "2030-01-01T00:00:00Z"),
        ("malformed", "20260905T00:00:00Z", "2030-01-01T00:00:00Z"),
    ] {
        let mut ctx = context.clone();
        ctx.valid_as_of = start.into();
        let mut inputs = contributions();
        inputs.last_mut().expect("expiry contribution").expiry_at = Some(end.into());
        let produced = compose_profile_runtime(
            &ctx,
            &profiles,
            &rules,
            &inputs,
            &[],
            "2026-09-05T20:00:02Z",
        )
        .expect("temporal composition");
        let owner = profile_runtime::resolve_policy_basis_v2(
            &ctx,
            &profiles,
            &rules,
            &produced,
            task_context(),
        );
        let result = match owner {
            Ok(value) => serde_json::json!({"accepted": true, "owner": value}),
            Err(_) => serde_json::json!({"accepted": false}),
        };
        temporal.insert(name.into(), result);
    }
    println!(
        "{}",
        serde_json::json!({"v1": v1, "v2": first, "other_task_v2": second, "temporal": temporal})
    );
}
