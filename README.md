# Sequence Review

Sequence Review is a local tool for grouping image sequences, comparing them side by side, and reviewing or moving frames with their YOLO labels. The sequence viewer shows a large frame and a thumbnail strip. The table shows first and last frames beside a scrollable strip for every sequence.

## Start with Docker Compose

Install and start Docker Desktop with Linux containers on Windows, or use Docker Engine with the Compose plugin on Linux. Run these commands from the repository root.

### Windows

```powershell
.\start.ps1 -DataFolder 'D:\Road-surface-irregularities-dataset' -ExportFolder 'D:\SequenceReviewExports'
```

The script builds and starts the container, then opens [Sequence Review](http://127.0.0.1:8765/). Use `-Port 8766` if port 8765 is already in use, or `-NoBrowser` to print the address without opening it.

### Docker Compose directly

Copy these settings into a file named `.env` at the repository root and adjust the paths. On Windows, use forward slashes.

```dotenv
SEQUENCE_REVIEW_DATA=D:/Road-surface-irregularities-dataset
SEQUENCE_REVIEW_EXPORTS=D:/SequenceReviewExports
SEQUENCE_REVIEW_PORT=8765
```

Use absolute paths for your machine, such as `/home/me/images` on Linux. With no configuration, Compose uses the repository's `data` and `exports` directories.

```sh
docker compose up --build -d --wait
docker compose logs -f
```

Open [http://127.0.0.1:8765/](http://127.0.0.1:8765/). The folder browser shows the mounted input under `/data`. Mount a parent folder if you want to choose between several datasets. The web interface is published on localhost.

## Open or sequence images

1. Browse to a folder and choose **Open existing sequences** or **Sequence a folder of images**.
2. For existing sequences, select **Last position**, **First unreviewed**, **First sequence**, or enter a specific sequence number. Last position is remembered by this browser for each workspace.
3. For automatic sequencing, choose whether to use filename numbers. Both modes use DINOv2 to find similar frames and SIFT geometric verification to confirm visual overlap. With numbers enabled, the tool also compares nearby numbers in the same filename family. With numbers disabled, it proposes matches from image content. Isolated images appear as single-frame sequences. Use the viewer or table to check the proposals and correct joins.
4. Click **Open this folder**. The progress bar follows the server job. Closing the browser tab leaves the running container and job active. The first sequencing run downloads [DINOv2 Small](https://huggingface.co/facebook/dinov2-small) into a Docker volume. CPU inference is the default in the supplied Docker image.

The importer understands folders such as `sequence_0001/*.jpg`, `images/sequence_0001/*.jpg` with mirrored `labels/sequence_0001/*.txt`, and the Nienaber layout with a `grouping_manifest.json` beside the `keep_together_*` folders. Select the actual dataset directory that contains the manifest. Supported images include JPG, PNG, WebP, BMP, and TIFF. YOLO TXT files can be mirrored under `labels` or sit next to the corresponding image. Global annotation formats such as COCO JSON are outside the exporter’s scope.

## Review, join, and export

- In **Sequence reviewer**, select frames and move them to an existing or new sequence. Flag frames or mark a sequence reviewed.
- In **Sequence table**, click **Join from** and **Join into** on the desired rows, then confirm the join. Click any image to open it in a same-tab viewer.
- Under **Folders / export**, enter a new folder name and click **Export images and labels**. The tool writes an organized copy with `images/sequence_####/`, matched `labels/sequence_####/`, and `sequence_review_export.json` mapping back to source frames. If an input image has no label, it is exported as an image only. Duplicate filenames in one sequence receive a suffix, which is also applied to the matching label.

The input mount is read-only. Joins, moves, flags, and notes are stored in the workspace catalog in the `review-state` volume and reflected in the exported copy. Reopening the same folder with the same workflow and filename setting resumes that catalog. The model cache is stored in the `model-cache` volume.

Use `docker compose stop` to stop the service after active jobs finish, and `docker compose up -d` to resume it. Removing Compose volumes removes saved catalogs and model weights. Export folders are stored on the host.

## License

Sequence Review is licensed under the [GNU Affero General Public License v3.0 only](LICENSE), SPDX identifier `AGPL-3.0-only`. The [SPDX license entry](https://spdx.org/licenses/AGPL-3.0-only.html) identifies this version. The application links to its license and source download from the folder screen. The DINOv2 weights and other dependencies retain their own licenses.
