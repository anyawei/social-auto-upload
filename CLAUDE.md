## Project Overview

This project, `social-auto-upload`, is a powerful automation tool designed to help content creators and operators efficiently publish video content to multiple domestic and international mainstream social media platforms in one click. The project implements video upload, scheduled release and other functions for platforms such as `Douyin`, `Bilibili`, `Xiaohongshu`, `Kuaishou`, `WeChat Channel`, `Baijiahao` and `TikTok`.

The project consists of a Python backend and a Vue.js frontend.

**Backend:**

*   Framework: Flask
*   Core Functionality:
    *   Handles file uploads and management.
    *   Interacts with a SQLite database to store information about files and user accounts.
    *   Uses `playwright` for browser automation to interact with social media platforms.
    *   Provides a RESTful API for the frontend to consume.
    *   Uses Server-Sent Events (SSE) for real-time communication with the frontend during the login process.

**Frontend:**

*   Framework: Vue.js
*   Build Tool: Vite
*   UI Library: Element Plus
*   State Management: Pinia
*   Routing: Vue Router
*   Core Functionality:
    *   Provides a web interface for managing social media accounts, video files, and publishing videos.
    *   Communicates with the backend via a RESTful API.

**Command-line Interface:**

The project also provides a command-line interface (CLI) for users who prefer to work from the terminal. For new Douyin CLI work, prefer the `sau douyin ...` entrypoint over legacy example scripts.

*   `login`: To log in to the Douyin uploader account.
*   `check`: To verify whether the saved Douyin cookie is still valid.
*   `upload`: To upload one video file with explicit metadata flags.
*   `stats`: Cross-platform — pull play/like/comment counts for published works (`sau stats --account <name> [--title <kw>] [--json]`). Reads official JSON APIs only (no DOM scraping): Douyin `work_list` (forced **headed** — headless returns an empty list), Kuaishou `photo/list`, Bilibili `member archives`. Implemented in `myUtils/stats.py`.

**一键发片(舞蹈成片)**:`uv run python myUtils/publish_dance.py --video <mp4> --dance "<舞蹈名>" [--role 沥] [--style 赛博朋克] [--dry-run]`。把"封面生成 + 标题/简介/标签"固化进发布流程,不靠记忆:
*   标题=简介=`{角色名}，跳个{舞蹈名}`;标签=`角色名,舞蹈名,风格,AI少女`。
*   封面由 `myUtils/cover_frames.py`(清晰度+正脸评分挑帧)自动生成:B站用横版(3帧横拼 `cover_landscape.jpg`),快手用竖版(最佳正脸帧 `cover_portrait.jpg`),抖音不传封面(自动首帧;传自定义图会报错)。
*   默认发 bilibili,douyin,kuaishou(账号「沄」);抖音强制 `--headed`;B站默认分区 tid=20(宅舞)。
*   `cover_frames.py` 也可单独跑只出封面:`uv run python myUtils/cover_frames.py <mp4>`。

## Building and Running

### Backend

1.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

2.  **Install Playwright browser drivers:**
    ```bash
    playwright install chromium
    ```

3.  **Initialize the database:**
    ```bash
    python db/createTable.py
    ```

4.  **Run the backend server:**
    ```bash
    python sau_backend.py
    ```
    The backend server will start on `http://localhost:5409`.

### Frontend

1.  **Navigate to the frontend directory:**
    ```bash
    cd sau_frontend
    ```

2.  **Install dependencies:**
    ```bash
    npm install
    ```

3.  **Run the development server:**
    ```bash
    npm run dev
    ```
    The frontend development server will start on `http://localhost:5173`.

### Command-line Interface

To use the CLI, you can run the `cli_main.py` script with the appropriate arguments.

**Login:**

```bash
sau douyin login --account <account_name>
```

**Check:**

```bash
sau douyin check --account <account_name>
```

**Upload:**

```bash
sau douyin upload --account <account_name> --file <video_file> --title <title> [--tags tag1,tag2] [--schedule YYYY-MM-DD HH:MM]
```

**Install bundled skill:**

```bash
sau skill install
```

## Development Conventions

*   The backend code is located in the root directory and the `myUtils` and `uploader` directories.
*   The frontend code is located in the `sau_frontend` directory.
*   The project uses a SQLite database for data storage. The database file is located at `db/database.db`.
*   The `conf.example.py` file should be copied to `conf.py` and configured with the appropriate settings.
*   The `requirements.txt` file lists the Python dependencies.
*   The `package.json` file in the `sau_frontend` directory lists the frontend dependencies.
