//! The tasks of the project. Run them with `cargo xtask <task>`:
//!
//! - `version`: print the version of the workspace.
//! - `check-pr BASE`: check a pull request that changes the version.
//! - `release-notes VERSION`: print the release notes of a version.
//! - `release-plan tag|upload ...`: decide the next step of the release job.
//! - `samples DIR`: write the synthetic sample images of the benchmark.
//! - `bench DIR`: time the command on the images of a folder.
//! - `sample-pixels IMAGE`: print random pixels of the fitted image, for excel_check.ps1.
//!
//! The release job runs the release tasks with `--no-default-features`. Then it does not
//! build the image code.

#[cfg(feature = "images")]
mod images;
mod release;

use std::io::Read;
use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "xtask", about = "The tasks of the Pixheet project")]
struct Args {
    #[command(subcommand)]
    task: Task,
}

#[derive(Subcommand)]
enum Task {
    /// Print the version in [workspace.package] of Cargo.toml.
    Version {
        #[arg(default_value = "Cargo.toml")]
        manifest: PathBuf,
    },
    /// Fail when a pull request changes the version to a version with a tag.
    CheckPr {
        /// Cargo.toml of the base branch.
        base: PathBuf,
        #[arg(default_value = "Cargo.toml")]
        manifest: PathBuf,
    },
    /// Print the release notes: the section of CHANGELOG.md, then the downloads.
    ReleaseNotes {
        version: String,
        #[arg(default_value = "CHANGELOG.md")]
        changelog: PathBuf,
    },
    /// Decide the next step of the release job. Read the functions in release.rs.
    ReleasePlan {
        #[command(subcommand)]
        step: PlanStep,
    },
    /// Print the names of the files of a release, one on each line.
    ReleaseFiles,
    /// Write the synthetic sample images of the benchmark.
    #[cfg(feature = "images")]
    Samples { dir: PathBuf },
    /// Time the command on each image of a folder.
    #[cfg(feature = "images")]
    Bench {
        dir: PathBuf,
        /// The pixheet binary.
        #[arg(long, default_value = "target/release/pixheet")]
        bin: PathBuf,
        /// The number of runs; the table gives the best.
        #[arg(short = 'n', long, default_value_t = 5)]
        runs: usize,
    },
    /// Print the fitted size and random pixels of an image as JSON.
    #[cfg(feature = "images")]
    SamplePixels {
        image: PathBuf,
        #[arg(long, default_value_t = 1000)]
        max_side: usize,
        #[arg(long, default_value = "white")]
        background: String,
        #[arg(long, default_value_t = 80)]
        count: usize,
        #[arg(long, default_value_t = 0)]
        seed: u64,
    },
}

#[derive(Subcommand)]
enum PlanStep {
    /// Print "create", "publish" or "skip" for the tag of the version.
    Tag {
        name: String,
        sha: String,
        /// The commit of the tag, or "" when the tag does not exist.
        tag_commit: String,
        /// "none", "draft" or "published".
        release: String,
    },
    /// Read the files of the release on stdin. Print the action, then the files to upload.
    Upload {
        release: String,
        #[arg(required = true)]
        files: Vec<String>,
    },
}

fn run(task: Task) -> Result<(), String> {
    match task {
        Task::Version { manifest } => println!("{}", release::read_version(&manifest)?),
        Task::CheckPr { base, manifest } => {
            let head = release::read_version(&manifest)?;
            let base = release::read_version(&base)?;
            let problems = release::pr_problems(&base, &head, release::remote_tag_exists)?;
            for problem in &problems {
                println!("::error::{problem}");
            }
            if !problems.is_empty() {
                return Err("the pull request has a problem".into());
            }
            if base == head {
                println!("check-pr: the pull request does not change the version {head}.");
            } else {
                println!("check-pr: the tag v{head} does not exist. The merge publishes it.");
            }
        }
        Task::ReleaseNotes { version, changelog } => {
            let text = std::fs::read_to_string(&changelog)
                .map_err(|e| format!("::error::Cannot read {}: {e}", changelog.display()))?;
            let notes =
                release::release_notes(&text, &version).map_err(|e| format!("::error::{e}"))?;
            print!("{notes}");
        }
        Task::ReleasePlan {
            step:
                PlanStep::Tag {
                    name,
                    sha,
                    tag_commit,
                    release,
                },
        } => match release::tag_action(&name, &tag_commit, &sha, &release) {
            Ok(action) => println!("{action}"),
            Err(e) => {
                println!("::error::{e}");
                return Err(String::new());
            }
        },
        Task::ReleasePlan {
            step: PlanStep::Upload { release, files },
        } => {
            let mut input = String::new();
            std::io::stdin()
                .read_to_string(&mut input)
                .map_err(|e| e.to_string())?;
            let existing: Vec<String> = input
                .lines()
                .map(str::trim)
                .filter(|l| !l.is_empty())
                .map(str::to_string)
                .collect();
            match release::upload_plan(&release, &existing, &files) {
                Ok((action, missing)) => {
                    println!("{action}");
                    for name in missing {
                        println!("{name}");
                    }
                }
                Err(e) => {
                    println!("::error::{e}");
                    return Err(String::new());
                }
            }
        }
        Task::ReleaseFiles => {
            for name in release::release_files() {
                println!("{name}");
            }
        }
        #[cfg(feature = "images")]
        Task::Samples { dir } => images::samples(&dir)?,
        #[cfg(feature = "images")]
        Task::Bench { dir, bin, runs } => images::bench(&dir, &bin, runs)?,
        #[cfg(feature = "images")]
        Task::SamplePixels {
            image,
            max_side,
            background,
            count,
            seed,
        } => {
            println!(
                "{}",
                images::sample_pixels(&image, max_side, &background, count, seed)?
            );
        }
    }
    Ok(())
}

fn main() -> ExitCode {
    match run(Args::parse().task) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            if !e.is_empty() {
                eprintln!("{e}");
            }
            ExitCode::FAILURE
        }
    }
}
