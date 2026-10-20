//! Disposable qualification peer, never a product CLI or authenticated transport.
//! Every command uses the actual canonical owner; no fabricated epochs or witnesses.
use semantic_memory::{
    AuthorityIssuer, AuthorityPermit, GovernedAccessPurposeV1, GovernedAccessRequestV1,
    MemoryConfig, MemoryStore, MockEmbedder,
};
use serde_json::json;
use std::io::{self, BufRead, Write};
use tempfile::TempDir;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let directory = TempDir::new()?;
    let config = MemoryConfig {
        base_dir: directory.path().to_path_buf(),
        ..Default::default()
    };
    let reader = MemoryStore::open_with_embedder(config.clone(), Box::new(MockEmbedder::new(768)))?;
    // Fixed disposable issuer input, not loaded from an operator or environment.
    let issuer = AuthorityIssuer::from_operator_token("ares-disposable-owner-qualification")
        .ok_or("invalid disposable issuer")?;
    let seeded = reader
        .authority()
        .append(
            issuer.mint_operator_system(
                "principal:ares",
                "qualification",
                AuthorityPermit::APPEND_CAPABILITY,
            ),
            "qualification-seed".into(),
            "public-memory".into(),
            "find evidence revocable sentinel".into(),
            None,
        )
        .await?;
    let fact = &seeded.affected_ids[0];
    // Independently opened writer; all state callbacks continue to use reader.
    let writer = MemoryStore::open_with_embedder(config, Box::new(MockEmbedder::new(768)))?;
    let access = GovernedAccessRequestV1::new(
        "principal:ares",
        "principal:ares",
        GovernedAccessPurposeV1::Recall,
        "public-memory",
    );
    println!("{}", json!({"access_request": access, "fact_id": fact}));
    io::stdout().flush()?;
    for line in io::stdin().lock().lines() {
        let command = line?;
        let response = match command.as_str() {
            "search" => serde_json::to_value(
                reader
                    .authority()
                    .search_governed_witnessed_v2(
                        "memory-request:1".into(),
                        "find evidence",
                        3,
                        access.clone(),
                    )
                    .await?,
            )?,
            "state" => serde_json::to_value(reader.authority().current_state().await?)?,
            "revoke" => {
                writer
                    .authority()
                    .revoke_origin(
                        issuer.mint_operator_system(
                            "principal:ares",
                            "qualification",
                            AuthorityPermit::REVOKE_ORIGIN_CAPABILITY,
                        ),
                        "qualification-revoke".into(),
                        fact,
                        "revocation:qualification".into(),
                    )
                    .await?;
                serde_json::to_value(reader.authority().current_state().await?)?
            }
            _ => return Err(format!("unsupported qualification command: {command}").into()),
        };
        println!("{response}");
        io::stdout().flush()?;
    }
    Ok(())
}
