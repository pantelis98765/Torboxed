# Torboxed

A Dockerized web application that seamlessly integrates [Torbox](https://torbox.app) with your media automation stack (Sonarr, Radarr, Whisparr). Upload NZB or torrent files to Torbox for processing, then automatically download the resulting files locally for your *arr applications to import.

## Features

- 🌐 **Web-based Interface**: Simple, intuitive web UI for uploading and managing downloads
- 🔄 **Torbox Integration**: Submit NZB and torrent files to Torbox API for processing
- 📥 **Automatic Downloads**: Automatically download completed files from Torbox to your local system
- 🎬 ***arr Compatibility**: Downloads are organized and ready for Sonarr/Radarr/Whisparr import
- ⚙️ **Configurable Settings**: Adjustable rate limits and download concurrency
- 🐳 **Dockerized**: Easy deployment with Docker Compose
- 💾 **Persistent Storage**: All data and downloads are persisted via Docker volumes
- 📊 **Download Management**: Track download status and history through the web interface

## Quick Start

### Prerequisites

- Docker and Docker Compose installed
- Torbox API key (optional, can be configured later)

### Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/pantelis98765/Torboxed.git
   cd Torboxed
   ```

2. (Optional) Set your Torbox API key in `docker-compose.yml`:
   ```yaml
   environment:
     - TORBOXED_TORBOX_API_KEY=your_api_key_here
   ```

3. Start the application:
   ```bash
   docker compose up --build
   ```

4. Open your browser and navigate to `http://localhost:8080`

## Configuration

### Environment Variables

You can configure Torboxed using environment variables (prefixed with `TORBOXED_`):

| Variable | Default | Description |
|----------|---------|-------------|
| `TORBOXED_DATA_DIR` | `/data` | Base directory for data storage |
| `TORBOXED_DB_PATH` | `/data/torboxed.db` | SQLite database path |
| `TORBOXED_DOWNLOAD_DIR` | `/data/downloads` | Directory for downloaded files |
| `TORBOXED_TORBOX_BASE_URL` | `https://api.torbox.app` | Torbox API base URL |
| `TORBOXED_TORBOX_API_KEY` | `None` | Your Torbox API key |
| `TORBOXED_TORBOX_RATE_LIMIT_PER_MINUTE` | `10` | Maximum API calls per minute |
| `TORBOXED_MAX_CONCURRENT_LOCAL_DOWNLOADS` | `2` | Maximum concurrent downloads |

### Settings via Web UI

You can also configure rate limits and download concurrency through the web interface's Settings page. Changes require a container restart to take effect.

## Usage

1. **Upload Files**: Use the web interface to upload NZB or torrent files
2. **Monitor Status**: Track your downloads as they're submitted to Torbox and downloaded locally
3. **Access Downloads**: Completed files are available in `/data/downloads` (or your configured download directory)
4. **Import to *arr**: Point your Sonarr/Radarr/Whisparr to the download directory for automatic import

## Architecture

- **Backend**: FastAPI-based Python application
- **Database**: SQLite for storing download history and settings
- **Frontend**: Modern web interface with real-time status updates
- **Download Worker**: Background worker handles concurrent downloads from Torbox
- **Rate Limiting**: Built-in rate limiting to respect Torbox API limits

## Project Structure

```
Torboxed/
├── docker-compose.yml      # Docker Compose configuration
├── Dockerfile              # Container image definition
├── requirements.txt        # Python dependencies
├── README.md              # This file
└── torboxed/              # Application source code
    ├── __init__.py
    ├── main.py            # FastAPI application and routes
    ├── config.py          # Configuration management
    ├── db.py              # Database models and setup
    ├── downloader.py      # Download worker implementation
    ├── torbox_client.py   # Torbox API client
    ├── arr_clients.py     # *arr application clients
    ├── static/            # Frontend assets
    │   ├── app.css
    │   └── app.js
    └── templates/         # HTML templates
        └── index.html
```

## Development

### Running Locally (without Docker)

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Set environment variables or create a `.env` file

3. Run the application:
   ```bash
   uvicorn torboxed.main:app --host 0.0.0.0 --port 8080
   ```

## Notes

- **API Endpoints**: The Torbox client endpoints in `torboxed/torbox_client.py` are currently placeholders (`/v1/submit` + `/v1/status/{id}`) until Torbox's exact API paths and fields are confirmed.
- **Rate Limiting**: Default rate limit is 10 API calls per minute. Adjust in Settings if needed.
- **Download Concurrency**: Control how many files download simultaneously via Settings.
- **Data Persistence**: All data (database, downloads, uploads) is stored in the Docker volume `torboxed_data`.

## License

This project is open source and available for use and modification.

## Contributing

Contributions, issues, and feature requests are welcome! Feel free to check the issues page.

## Support

For issues, questions, or contributions, please open an issue on GitHub.
