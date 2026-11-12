use std::sync::atomic::Ordering;

use super::*;
use crate::files::Existing;
use crate::worker::Message;

type RunArgs = (Arc<Queue>, Arc<Mutex<Options>>, Arc<AtomicBool>, u64);

/// A spawner that keeps the arguments of each run. run_all() then runs the worker on
/// the thread of the test.
fn deferred() -> (Spawner, Arc<Mutex<Vec<RunArgs>>>) {
    let runs = Arc::new(Mutex::new(Vec::new()));
    let keep = Arc::clone(&runs);
    let spawn: Spawner = Box::new(move |q, o, c, w| keep.lock().unwrap().push((q, o, c, w)));
    (spawn, runs)
}

fn run_all(batch: &mut Batch, runs: &Arc<Mutex<Vec<RunArgs>>>) {
    loop {
        let next = runs.lock().unwrap().pop();
        let Some((queue, options, cancel, worker)) = next else {
            return;
        };
        let mut messages = Vec::new();
        crate::worker::run(queue, options, cancel, &mut |m| messages.push(m));
        for message in messages {
            batch.handle(worker, message);
        }
    }
}

fn png(path: &Path, w: u32, h: u32) {
    image::RgbImage::from_fn(w, h, |x, y| image::Rgb([x as u8 * 40, y as u8 * 50, 7]))
        .save(path)
        .unwrap();
}

fn images(dir: &Path, count: usize) -> Vec<PathBuf> {
    (0..count)
        .map(|i| {
            let path = dir.join(format!("img{i}.png"));
            png(&path, 4, 3);
            path
        })
        .collect()
}

fn manual() -> Settings {
    Settings {
        auto_start: false,
        ..Settings::default()
    }
}

#[test]
fn a_drop_adds_cards_and_starts_one_run() {
    let dir = tempfile::tempdir().unwrap();
    let (spawn, runs) = deferred();
    let mut batch = Batch::new(spawn, Settings::default());
    batch.add(&images(dir.path(), 3));
    let updates = batch.take_updates();
    let cards = updates.cards.expect("all cards");
    assert_eq!(cards.len(), 3);
    assert!(
        cards
            .iter()
            .all(|c| c.state == State::Ready && c.status == "Ready")
    );
    assert_eq!(runs.lock().unwrap().len(), 1);
    assert!(updates.summary.running);
    assert_eq!(updates.summary.text, "Converting 1 of 3");
}

#[test]
fn without_auto_start_the_cards_wait_for_start() {
    let dir = tempfile::tempdir().unwrap();
    let (spawn, runs) = deferred();
    let mut batch = Batch::new(spawn, manual());
    batch.add(&images(dir.path(), 2));
    assert!(runs.lock().unwrap().is_empty());
    assert_eq!(batch.take_updates().summary.text, "2 images ready");
    batch.start();
    assert_eq!(runs.lock().unwrap().len(), 1);
}

#[test]
fn a_run_converts_and_gives_previews() {
    let dir = tempfile::tempdir().unwrap();
    let (spawn, runs) = deferred();
    let mut batch = Batch::new(spawn, Settings::default());
    let paths = images(dir.path(), 3);
    batch.add(&paths);
    batch.take_updates();
    run_all(&mut batch, &runs);
    let updates = batch.take_updates();
    assert_eq!(batch.counts.done, 3);
    assert_eq!(updates.previews.len(), 3);
    assert!(
        updates
            .previews
            .iter()
            .all(|(_, url)| url.starts_with("data:image/png;base64,"))
    );
    assert_eq!(updates.summary.text, "Converted 3 images");
    assert_eq!(
        updates.summary.folder.as_deref(),
        Some(dir.path().to_str().unwrap())
    );
    assert!(paths.iter().all(|p| p.with_extension("xlsx").is_file()));
    let done = &updates.changed[0];
    assert_eq!(done.status, "Done");
    assert!(
        done.detail.starts_with("4 × 3 → 4 × 3\n12 colors · "),
        "{}",
        done.detail
    );
}

#[test]
fn a_tick_of_500_cards_changes_one_card() {
    let dir = tempfile::tempdir().unwrap();
    let (spawn, _) = deferred();
    let mut batch = Batch::new(spawn, Settings::default());
    batch.add(&images(dir.path(), 500));
    batch.take_updates();
    let worker = batch.worker;
    let id = batch.order[250];
    batch.handle(worker, Message::Started { id });
    batch.handle(worker, Message::Progress { id, percent: 40 });
    let updates = batch.take_updates();
    assert!(updates.cards.is_none());
    assert_eq!(updates.changed.len(), 1);
    assert_eq!(updates.changed[0].status, "Writing 40%");
    assert_eq!(batch.counts.converting, 1);
    assert_eq!(batch.counts.ready, 499);
}

#[test]
fn two_images_with_one_name_get_two_outputs() {
    let dir = tempfile::tempdir().unwrap();
    let (a, b) = (dir.path().join("photo.png"), dir.path().join("photo.bmp"));
    png(&a, 2, 2);
    png(&b, 2, 2);
    let (spawn, _) = deferred();
    let mut batch = Batch::new(spawn, manual());
    batch.add(&[a, b]);
    let cards = batch.take_updates().cards.unwrap();
    assert!(cards[0].dst.ends_with("photo.xlsx"));
    assert!(cards[1].dst.ends_with("photo (2).xlsx"));
    assert_eq!(cards[1].detail, "Writes photo (2).xlsx");
}

#[test]
fn the_policy_skip_keeps_an_existing_output() {
    let dir = tempfile::tempdir().unwrap();
    let paths = images(dir.path(), 2);
    std::fs::write(paths[0].with_extension("xlsx"), b"old").unwrap();
    let (spawn, runs) = deferred();
    let settings = Settings {
        existing: Existing::Skip,
        ..Settings::default()
    };
    let mut batch = Batch::new(spawn, settings);
    batch.add(&paths);
    run_all(&mut batch, &runs);
    assert_eq!(batch.counts.skipped, 1);
    assert_eq!(batch.counts.done, 1);
    assert_eq!(
        std::fs::read(paths[0].with_extension("xlsx")).unwrap(),
        b"old"
    );
    let text = batch.take_updates().summary.text;
    assert_eq!(text, "Converted 1 of 2 images · 1 skipped");
}

#[test]
fn a_failed_image_tells_why_and_can_retry() {
    let dir = tempfile::tempdir().unwrap();
    let bad = dir.path().join("bad.png");
    std::fs::write(&bad, b"not an image").unwrap();
    let (spawn, runs) = deferred();
    let mut batch = Batch::new(spawn, Settings::default());
    batch.add(std::slice::from_ref(&bad));
    batch.take_updates();
    run_all(&mut batch, &runs);
    let updates = batch.take_updates();
    let card = &updates.changed[0];
    assert_eq!(card.state, State::Failed);
    assert_eq!(card.status, "Not an image");
    assert_eq!(updates.summary.text, "Converted 0 of 1 image · 1 failed");
    png(&bad, 2, 2);
    batch.retry_failed();
    run_all(&mut batch, &runs);
    assert_eq!(batch.counts.done, 1);
}

#[test]
fn stop_leaves_the_other_images_ready() {
    let dir = tempfile::tempdir().unwrap();
    let (spawn, runs) = deferred();
    let mut batch = Batch::new(spawn, Settings::default());
    batch.add(&images(dir.path(), 3));
    let worker = batch.worker;
    let first = batch.order[0];
    batch.handle(worker, Message::Started { id: first });
    batch.stop();
    assert!(batch.take_updates().summary.stopping);
    batch.handle(worker, Message::Stopped { id: first });
    batch.handle(worker, Message::Finished { stopped: true });
    let updates = batch.take_updates();
    assert!(!updates.summary.running);
    assert_eq!(batch.counts.ready, 2);
    assert_eq!(
        updates.summary.text,
        "Stopped · Converted 0 of 3 images · 1 stopped · 2 ready"
    );
    // The next start queues the images again, also the ones of the old run.
    runs.lock().unwrap().clear();
    batch.retry_failed();
    assert_eq!(batch.counts.ready, 3);
    run_all(&mut batch, &runs);
    assert_eq!(batch.counts.done, 3);
}

#[test]
fn a_closing_window_takes_no_new_work() {
    let dir = tempfile::tempdir().unwrap();
    let paths = images(dir.path(), 2);
    let (spawn, runs) = deferred();
    let mut batch = Batch::new(spawn, Settings::default());
    batch.add(&paths[..1]);
    let worker = batch.worker;
    batch.closing = true;
    batch.add(&paths[1..]);
    assert_eq!(batch.counts.total, 1);
    // The job of the first image is still in the queue. Without the close, the end of
    // the run would start a new worker for it.
    batch.handle(worker, Message::Finished { stopped: false });
    assert!(!batch.running());
    assert_eq!(runs.lock().unwrap().len(), 1, "no new worker");
    // The first image is ready, but the closing window does not start it.
    assert_eq!(batch.counts.ready, 1);
    batch.start();
    assert!(!batch.running());
    assert_eq!(runs.lock().unwrap().len(), 1, "no new run");
}

#[test]
fn images_that_come_at_the_end_of_a_run_start_a_new_worker() {
    let dir = tempfile::tempdir().unwrap();
    let paths = images(dir.path(), 2);
    let (spawn, runs) = deferred();
    let mut batch = Batch::new(spawn, Settings::default());
    batch.add(&paths[..1]);
    // The worker converts the first image. The second image comes before the batch gets
    // the last message of the worker, so the worker did not see it.
    let (queue, options, cancel, worker) = runs.lock().unwrap().pop().unwrap();
    let mut messages = Vec::new();
    crate::worker::run(queue, options, cancel, &mut |m| messages.push(m));
    batch.add(&paths[1..]);
    assert!(runs.lock().unwrap().is_empty(), "the run goes on");
    for message in messages {
        batch.handle(worker, message);
    }
    assert!(batch.running());
    run_all(&mut batch, &runs);
    assert_eq!(batch.counts.done, 2);
}

#[test]
fn a_removed_card_ignores_the_messages_of_the_worker() {
    let dir = tempfile::tempdir().unwrap();
    let (spawn, runs) = deferred();
    let mut batch = Batch::new(spawn, Settings::default());
    batch.add(&images(dir.path(), 2));
    let worker = batch.worker;
    let (first, second) = (batch.order[0], batch.order[1]);
    // The reader takes the two jobs out of the queue.
    let queue = Arc::clone(&runs.lock().unwrap()[0].0);
    let (_job1, job2) = (queue.take().unwrap(), queue.take().unwrap());
    // A card that converts stays in the list.
    batch.handle(worker, Message::Started { id: first });
    batch.remove(&[first]);
    assert_eq!(batch.counts.converting, 1);
    // The reader has the removed job already: the job must stop.
    batch.remove(&[second]);
    assert!(job2.skip.load(Ordering::SeqCst));
    let url = "data:image/png;base64,".to_string();
    batch.handle(worker, Message::Started { id: second });
    batch.handle(worker, Message::Preview { id: second, url });
    batch.handle(worker, Message::Stopped { id: second });
    assert_eq!(batch.counts.total, 1);
    let updates = batch.take_updates();
    assert!(updates.previews.is_empty());
    let cards = updates.cards.unwrap();
    assert_eq!(cards.iter().map(|c| c.id).collect::<Vec<_>>(), [first]);
}

#[test]
fn a_message_of_an_old_worker_has_no_effect() {
    let dir = tempfile::tempdir().unwrap();
    let (spawn, _) = deferred();
    let mut batch = Batch::new(spawn, Settings::default());
    batch.add(&images(dir.path(), 1));
    let id = batch.order[0];
    batch.handle(batch.worker + 1, Message::Started { id });
    assert_eq!(batch.counts.converting, 0);
}

#[test]
fn a_drop_tells_about_other_files_and_duplicates() {
    let dir = tempfile::tempdir().unwrap();
    let paths = images(dir.path(), 1);
    std::fs::write(dir.path().join("notes.txt"), b"x").unwrap();
    let (spawn, _) = deferred();
    let mut batch = Batch::new(spawn, manual());
    batch.add(&[dir.path().to_path_buf()]);
    assert_eq!(
        batch.take_updates().toast.as_deref(),
        Some("Ignored 1 file that is not an image.")
    );
    batch.add(&paths);
    assert_eq!(
        batch.take_updates().toast.as_deref(),
        Some("1 image is in the list already.")
    );
    batch.add(&[dir.path().join("notes.txt")]);
    let toast = batch.take_updates().toast.unwrap();
    assert_eq!(
        toast,
        "The drop has no images. Ignored 1 file that is not an image."
    );
    std::fs::write(dir.path().join("more.txt"), b"x").unwrap();
    let missing = dir.path().join("gone.png");
    batch.add(&[
        dir.path().join("notes.txt"),
        dir.path().join("more.txt"),
        missing,
    ]);
    let toast = batch.take_updates().toast.unwrap();
    assert_eq!(
        toast,
        "The drop has no images. Ignored 2 files that are not images. \
         The app cannot read 1 dropped item."
    );
}

#[test]
fn a_new_output_folder_moves_the_ready_outputs() {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("out");
    let (spawn, _) = deferred();
    let mut batch = Batch::new(spawn, manual());
    batch.add(&images(dir.path(), 2));
    batch.take_updates();
    batch.set_settings(Settings {
        out_dir: Some(out.clone()),
        ..manual()
    });
    let updates = batch.take_updates();
    assert_eq!(updates.changed.len(), 2);
    assert!(
        updates
            .changed
            .iter()
            .all(|c| Path::new(&c.dst).parent() == Some(out.as_path()))
    );
    batch.set_settings(Settings {
        background: "nope".into(),
        ..manual()
    });
    assert!(
        batch
            .take_updates()
            .error
            .unwrap()
            .starts_with("invalid background color")
    );
}

#[test]
fn convert_again_writes_over_its_own_output() {
    let dir = tempfile::tempdir().unwrap();
    let (spawn, runs) = deferred();
    let mut batch = Batch::new(spawn, Settings::default());
    let paths = images(dir.path(), 1);
    batch.add(&paths);
    run_all(&mut batch, &runs);
    let id = batch.order[0];
    batch.convert_again(&[id]);
    run_all(&mut batch, &runs);
    assert_eq!(batch.counts.done, 1);
    assert!(!dir.path().join("img0 (2).xlsx").exists());
}

#[test]
fn convert_again_keeps_the_assigned_output() {
    // Keep both gave photo (2).xlsx, because an unrelated photo.xlsx exists.
    let dir = tempfile::tempdir().unwrap();
    let src = dir.path().join("photo.png");
    png(&src, 3, 2);
    let other = dir.path().join("photo.xlsx");
    std::fs::write(&other, b"other").unwrap();
    let (spawn, runs) = deferred();
    let mut batch = Batch::new(spawn, Settings::default());
    batch.add(std::slice::from_ref(&src));
    run_all(&mut batch, &runs);
    let id = batch.order[0];
    batch.convert_again(&[id]);
    run_all(&mut batch, &runs);
    assert_eq!(batch.counts.done, 1);
    assert_eq!(std::fs::read(&other).unwrap(), b"other");
    assert_eq!(batch.items[&id].dst, dir.path().join("photo (2).xlsx"));
}

#[test]
fn start_plans_again_an_output_that_appeared() {
    let dir = tempfile::tempdir().unwrap();
    let src = dir.path().join("photo.png");
    png(&src, 3, 2);
    let (spawn, runs) = deferred();
    let mut batch = Batch::new(spawn, manual());
    batch.add(std::slice::from_ref(&src));
    let id = batch.order[0];
    assert_eq!(batch.items[&id].dst, dir.path().join("photo.xlsx"));
    // The file appears before the click on Convert.
    std::fs::write(dir.path().join("photo.xlsx"), b"other").unwrap();
    batch.start();
    assert_eq!(batch.items[&id].dst, dir.path().join("photo (2).xlsx"));
    run_all(&mut batch, &runs);
    assert_eq!(
        std::fs::read(dir.path().join("photo.xlsx")).unwrap(),
        b"other"
    );
    // With Skip, the image does not convert.
    let mut batch = Batch::new(
        deferred().0,
        Settings {
            existing: Existing::Skip,
            ..manual()
        },
    );
    batch.add(std::slice::from_ref(&src));
    std::fs::remove_file(dir.path().join("photo (2).xlsx")).unwrap();
    batch.start();
    assert_eq!(batch.counts.skipped, 1);
}

#[test]
fn the_worker_does_not_write_over_a_file_that_appeared() {
    let dir = tempfile::tempdir().unwrap();
    let src = dir.path().join("photo.png");
    png(&src, 3, 2);
    let (spawn, runs) = deferred();
    let mut batch = Batch::new(spawn, Settings::default());
    batch.add(std::slice::from_ref(&src));
    batch.take_updates();
    // The job waits in the queue, and then the file appears.
    std::fs::write(dir.path().join("photo.xlsx"), b"other").unwrap();
    run_all(&mut batch, &runs);
    let card = batch.take_updates().changed.pop().unwrap();
    assert_eq!(card.status, "File exists");
    assert_eq!(
        std::fs::read(dir.path().join("photo.xlsx")).unwrap(),
        b"other"
    );
    // Convert again of a failed image plans a new output with the current policy. It
    // does not write over the file that appeared.
    batch.convert_again(&[card.id]);
    run_all(&mut batch, &runs);
    assert_eq!(batch.counts.done, 1);
    assert_eq!(
        std::fs::read(dir.path().join("photo.xlsx")).unwrap(),
        b"other"
    );
    assert!(dir.path().join("photo (2).xlsx").is_file());
}

#[test]
fn a_new_output_folder_during_a_run_moves_the_waiting_jobs() {
    let dir = tempfile::tempdir().unwrap();
    let out = dir.path().join("out");
    std::fs::create_dir(&out).unwrap();
    let (spawn, runs) = deferred();
    let mut batch = Batch::new(spawn, Settings::default());
    let paths = images(dir.path(), 3);
    batch.add(&paths);
    assert!(batch.running());
    batch.set_settings(Settings {
        out_dir: Some(out.clone()),
        ..Settings::default()
    });
    run_all(&mut batch, &runs);
    assert_eq!(batch.counts.done, 3);
    for path in &paths {
        assert!(
            out.join(path.with_extension("xlsx").file_name().unwrap())
                .is_file()
        );
        assert!(!path.with_extension("xlsx").exists());
    }
}

#[test]
fn clear_removes_all_cards_without_a_run() {
    let dir = tempfile::tempdir().unwrap();
    let (spawn, _runs) = deferred();
    let mut batch = Batch::new(spawn, manual());
    batch.add(&images(dir.path(), 2));
    assert_eq!(batch.counts.ready, 2);
    batch.clear();
    let updates = batch.take_updates();
    assert_eq!(updates.cards.map(|c| c.len()), Some(0));
    assert_eq!(updates.summary.counts.total, 0);
}
