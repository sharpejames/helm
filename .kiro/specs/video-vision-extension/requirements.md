# Requirements Document

## Introduction

A Chrome extension for the Helm desktop automation agent that enables real-time AI-powered video description. The Extension allows users to select any `<video>` element on a web page, capture frames directly from the DOM via the Canvas API, and stream those frames to the Helm backend (localhost:8765) for vision analysis using local Qwen models via Ollama. The Extension provides a side panel UI with live commentary, alert configuration, text-to-speech output, and desktop notifications. This replaces the existing mss-based screen capture approach with direct pixel access from the video element, yielding higher quality frames that work regardless of window visibility or screen position.

## Glossary

- **Extension**: The Chrome browser extension that lives in the `extension/` directory at the project root
- **Side_Panel**: The Chrome side panel UI rendered by the Extension, displaying commentary, alerts, and controls
- **Video_Selector**: The interactive overlay mode that highlights `<video>` elements on the page and lets the user click to select one
- **Frame_Capturer_Client**: The content script component that captures frames from the selected video element using the Canvas API and encodes them as base64 PNG
- **Helm_Backend**: The existing FastAPI server running on localhost:8765 that hosts video analysis endpoints
- **Commentary_Stream**: The real-time text feed of AI-generated scene descriptions delivered over WebSocket from the Helm_Backend
- **Alert_Condition**: A user-defined watch phrase (e.g., "coyote", "person at door") that triggers a desktop notification when matched in a description
- **TTS_Engine**: The browser's built-in Web Speech API (SpeechSynthesis) used to read commentary aloud
- **Extension_Frame_Endpoint**: A new or modified Helm_Backend WebSocket endpoint that accepts base64-encoded frames from the Extension instead of capturing frames via mss
- **Context_Commentary**: The mode where the LLM maintains awareness of previous descriptions and only narrates meaningful scene changes

## Requirements

### Requirement 1: Video Element Selection

**User Story:** As a user, I want to click on any video playing on a web page and select it for analysis, so that I can get AI descriptions of that specific video without manual coordinate entry.

#### Acceptance Criteria

1. WHEN the user clicks the "Select Video" button in the Side_Panel, THE Extension SHALL inject the Video_Selector overlay into the active tab
2. WHILE the Video_Selector is active, THE Extension SHALL highlight each `<video>` element on the page with a visible border when the user hovers over the element
3. WHEN the user clicks a highlighted `<video>` element, THE Video_Selector SHALL store a reference to that element and report its dimensions back to the Side_Panel
4. IF no `<video>` elements exist on the current page, THEN THE Extension SHALL display a message in the Side_Panel stating that no video elements were found
5. WHEN a video element is selected, THE Side_Panel SHALL display the video dimensions and a thumbnail preview of the current frame

### Requirement 2: Frame Capture via Canvas API

**User Story:** As a user, I want the extension to capture frames directly from the video element's pixel data, so that capture works regardless of window position, overlapping windows, or background tabs.

#### Acceptance Criteria

1. WHEN a capture session is active, THE Frame_Capturer_Client SHALL draw the selected video element onto an offscreen Canvas at the configured frame rate
2. THE Frame_Capturer_Client SHALL encode each captured canvas frame as a base64 PNG string
3. THE Frame_Capturer_Client SHALL capture frames at a configurable rate between 0.5 and 2.0 frames per second, defaulting to 1.0 FPS
4. IF the selected video element is removed from the DOM or its source changes, THEN THE Frame_Capturer_Client SHALL stop capture and notify the Side_Panel with a descriptive status message
5. WHEN the video element is paused, THE Frame_Capturer_Client SHALL pause frame capture and resume capture when the video element resumes playback
6. THE Frame_Capturer_Client SHALL resize captured frames to a maximum dimension of 1024 pixels on the longest side while preserving the aspect ratio, to limit bandwidth to the Helm_Backend

### Requirement 3: WebSocket Communication with Helm Backend

**User Story:** As a user, I want the extension to stream captured frames to my local Helm backend for AI analysis, so that I get real-time descriptions without cloud dependencies.

#### Acceptance Criteria

1. WHEN a capture session starts, THE Extension SHALL open a WebSocket connection to the Extension_Frame_Endpoint on the Helm_Backend at ws://localhost:8765/api/video/extension-stream
2. THE Extension SHALL send each captured frame as a JSON message containing the base64 PNG data and a Unix timestamp over the WebSocket connection
3. WHEN the Helm_Backend sends a commentary message over the WebSocket, THE Extension SHALL parse the JSON payload and deliver the description text and timestamp to the Side_Panel
4. IF the WebSocket connection drops, THEN THE Extension SHALL attempt to reconnect up to 5 times with exponential backoff starting at 1 second
5. IF all 5 reconnection attempts fail, THEN THE Extension SHALL display a connection error in the Side_Panel and stop frame capture
6. WHEN the user stops the capture session, THE Extension SHALL send a stop message over the WebSocket and close the connection cleanly

### Requirement 4: Backend Extension Frame Endpoint

**User Story:** As a developer, I want the Helm backend to accept frames pushed from the Chrome extension over WebSocket, so that the existing vision pipeline processes extension-sourced frames identically to mss-captured frames.

#### Acceptance Criteria

1. THE Helm_Backend SHALL expose a WebSocket endpoint at /api/video/extension-stream that accepts JSON messages containing a base64-encoded PNG frame and a timestamp
2. WHEN the Extension_Frame_Endpoint receives a frame message, THE Helm_Backend SHALL decode the base64 PNG and pass the frame bytes to VisionModule.describe_frame for analysis
3. WHEN VisionModule.describe_frame returns a description, THE Helm_Backend SHALL push the description through the existing EventDetector, AlertSystem, and CommentaryStream pipeline
4. THE Helm_Backend SHALL send each commentary entry back to the connected Extension client as a JSON message over the same WebSocket connection
5. WHEN the Extension sends a stop message, THE Helm_Backend SHALL clean up the session resources and close the WebSocket connection
6. IF the Extension disconnects without sending a stop message, THEN THE Helm_Backend SHALL detect the disconnection and clean up session resources within 5 seconds


### Requirement 5: Side Panel UI

**User Story:** As a user, I want a persistent side panel in Chrome showing live descriptions, history, and controls, so that I can monitor video analysis without switching tabs.

#### Acceptance Criteria

1. THE Side_Panel SHALL display a "Select Video" button, a "Start/Stop" toggle button, and an FPS slider (0.5–2.0, default 1.0)
2. WHEN a capture session is active, THE Side_Panel SHALL display each new commentary entry in a scrollable live feed with a timestamp
3. THE Side_Panel SHALL maintain a full text history log of all commentary entries received during the current browser session
4. THE Side_Panel SHALL display the current connection status to the Helm_Backend as one of: "Disconnected", "Connecting", "Connected", or "Error"
5. WHEN the user clicks the "Start" button with a video element selected, THE Side_Panel SHALL initiate frame capture and open the WebSocket connection to the Helm_Backend
6. WHEN the user clicks the "Stop" button, THE Side_Panel SHALL stop frame capture and close the WebSocket connection

### Requirement 6: Alert Configuration and Notifications

**User Story:** As a user, I want to define watch conditions and receive desktop notifications when those conditions appear in the video, so that I can be alerted to specific events without watching continuously.

#### Acceptance Criteria

1. THE Side_Panel SHALL provide a text input and "Add" button for the user to add Alert_Condition watch phrases
2. THE Side_Panel SHALL display all configured Alert_Conditions as a list with a remove button for each condition
3. WHEN a capture session starts, THE Extension SHALL send the list of configured Alert_Conditions to the Helm_Backend as part of the session configuration
4. WHEN the Helm_Backend detects a match for an Alert_Condition in a commentary description, THE Helm_Backend SHALL include an alert flag and the matched condition in the WebSocket message to the Extension
5. WHEN the Extension receives an alert-flagged message, THE Extension SHALL trigger a Chrome desktop notification containing the matched condition and the description text
6. THE Extension SHALL request the Chrome notifications permission at install time and display a prompt in the Side_Panel if the permission is denied

### Requirement 7: Context-Aware Commentary

**User Story:** As a user, I want the AI to maintain awareness of what it previously described and only narrate meaningful changes, so that the commentary stream is informative rather than repetitive.

#### Acceptance Criteria

1. THE Helm_Backend SHALL maintain a sliding window of the last 10 commentary descriptions for each active extension session
2. WHEN generating a description for a new frame, THE Helm_Backend SHALL include the last 3 descriptions as context in the VisionModule prompt so the model can reference previous scene state
3. THE Helm_Backend SHALL instruct the VisionModule to describe only meaningful changes relative to the previous descriptions, rather than re-describing the entire scene
4. THE CommentaryStream SHALL suppress consecutive duplicate descriptions, forwarding only entries where the description text differs from the previous entry

### Requirement 8: Text-to-Speech Audio Output

**User Story:** As a user, I want to hear the commentary read aloud, so that I can listen to video descriptions without reading the screen.

#### Acceptance Criteria

1. THE Side_Panel SHALL provide a TTS toggle button that enables or disables audio output of commentary entries
2. WHEN TTS is enabled and a new commentary entry arrives, THE TTS_Engine SHALL speak the description text using the browser Web Speech API (SpeechSynthesis)
3. WHILE the TTS_Engine is speaking a previous entry, THE TTS_Engine SHALL queue the new entry and speak it after the current utterance completes
4. THE Side_Panel SHALL provide a voice selection dropdown populated with the voices available from the browser SpeechSynthesis API
5. THE Side_Panel SHALL provide a speech rate slider with a range of 0.5 to 2.0, defaulting to 1.0

### Requirement 9: Extension Manifest and Permissions

**User Story:** As a developer, I want the extension to declare only the minimum required Chrome permissions, so that users can trust the extension with their browser.

#### Acceptance Criteria

1. THE Extension manifest.json SHALL declare Manifest V3 format with the sidePanel, activeTab, scripting, and notifications permissions
2. THE Extension SHALL use a content script injected via chrome.scripting.executeScript only when the user activates the Video_Selector, rather than injecting into all pages by default
3. THE Extension SHALL connect only to localhost origins (ws://localhost:8765) and SHALL declare this host in the manifest host_permissions
4. THE Extension SHALL include a 128x128 icon and a descriptive name ("Helm Video Vision") in the manifest

### Requirement 10: Extension Project Structure

**User Story:** As a developer, I want the extension code organized in a standard Chrome extension layout within the project, so that it is easy to load as an unpacked extension during development.

#### Acceptance Criteria

1. THE Extension source files SHALL reside in the `extension/` directory at the Helm project root
2. THE Extension SHALL contain at minimum: manifest.json, a service worker background script, a content script for video selection and frame capture, and a side panel HTML page with associated JavaScript and CSS
3. THE Extension SHALL load successfully as an unpacked extension in Chrome via chrome://extensions with Developer Mode enabled
