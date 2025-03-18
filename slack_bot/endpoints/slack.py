import json
import os
import traceback
import requests
from typing import Mapping, List, Dict, Any, Optional
from werkzeug import Request, Response
from dify_plugin import Endpoint
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dify_plugin.invocations.file import UploadFileResponse


class SlackEndpoint(Endpoint):
    def _invoke(self, r: Request, values: Mapping, settings: Mapping) -> Response:
        """
        Invokes the endpoint with the given request.
        """
        # Early return for retry requests if not allowed
        if self._should_ignore_retry(r, settings):
            return self._ok_response()

        data = r.get_json()

        # Handle Slack URL verification challenge
        if data.get("type") == "url_verification":
            return self._handle_verification(data)

        # Handle event callbacks
        if data.get("type") == "event_callback":
            return self._handle_event_callback(data, settings)

        # Default response for unhandled event types
        return self._ok_response()

    def _should_ignore_retry(self, r: Request, settings: Mapping) -> bool:
        """Check if we should ignore this retry request."""
        retry_num = r.headers.get("X-Slack-Retry-Num")
        retry_reason = r.headers.get("X-Slack-Retry-Reason")

        return not settings.get("allow_retry") and (
            retry_reason == "http_timeout"
            or (retry_num is not None and int(retry_num) > 0)
        )

    def _handle_verification(self, data: Dict) -> Response:
        """Handle Slack URL verification challenge."""
        return Response(
            response=json.dumps({"challenge": data.get("challenge")}),
            status=200,
            content_type="application/json",
        )

    def _ok_response(self) -> Response:
        """Return a standard OK response."""
        return Response(status=200, response="ok")

    def _handle_event_callback(self, data: Dict, settings: Mapping) -> Response:
        """Process Slack event callbacks."""
        print(f"data: {data}")
        event = data.get("event", {})
        event_type = event.get("type")
        user = event.get("user", "")

        # Check if we should ignore this user
        if self._should_ignore_user(user, settings):
            return self._ok_response()

        print(f"event_type: {event_type}")

        # Get Slack client
        token = settings.get("bot_token")
        client = WebClient(token=token)

        # Check channel configuration
        channel_id = event.get("channel", "")
        if not self._is_configured_channel(channel_id, settings, client):
            return self._ok_response()

        # Determine if we should process this event
        should_process, message, blocks = self._prepare_event_data(
            event, event_type, settings
        )

        if should_process and message:
            try:
                return self._process_message(
                    event, event_type, message, blocks, channel_id, client, settings
                )
            except SlackApiError as slack_error:
                print(f"Slack API Error: {slack_error}")
                raise slack_error
            except Exception as e:
                return self._handle_processing_error(e, event, channel_id, client)

        return self._ok_response()

    def _should_ignore_user(self, user: str, settings: Mapping) -> bool:
        """Check if a user should be ignored based on settings."""
        if not user:
            return True

        ignore_user_ids_str = settings.get("ignore_user_ids", "")
        ignore_user_ids = (
            [user_id.strip() for user_id in ignore_user_ids_str.split(",")]
            if ignore_user_ids_str
            else []
        )

        return user in ignore_user_ids

    def _is_configured_channel(
        self, channel_id: str, settings: Mapping, client: WebClient
    ) -> bool:
        """Check if the event is from the configured channel."""
        configured_channel_name = settings.get("channel_name", "")

        print(f"channel_id: {channel_id}")
        print(f"configured_channel_name: {configured_channel_name}")

        if not configured_channel_name:
            return True  # No channel configured, so all channels are valid

        try:
            channel_info = client.conversations_info(channel=channel_id)
            print(f"channel_info: {channel_info}")

            if channel_info["channel"]["name"] != configured_channel_name:
                print(f"Not the configured channel: {channel_info['channel']['name']}")
                return False

            return True
        except SlackApiError as e:
            print(f"Error getting channel info: {e}")
            return True  # If we can't get channel info, continue processing

    def _prepare_event_data(
        self, event: Dict, event_type: str, settings: Mapping
    ) -> tuple:
        """Prepare event data for processing."""
        configured_event_types = settings.get("event_types", "app_mention")
        should_process = False
        message = ""
        blocks = []

        print(f"Initial should_process: {should_process}")

        if event_type == "app_mention" and (
            configured_event_types == "app_mention" or configured_event_types == "both"
        ):
            should_process, message, blocks = self._prepare_app_mention_data(event)
        elif event_type == "message" and (
            configured_event_types == "message" or configured_event_types == "both"
        ):
            should_process, message, blocks = self._prepare_message_data(event)

        return should_process, message, blocks

    def _prepare_app_mention_data(self, event: Dict) -> tuple:
        """Prepare data from an app_mention event."""
        message = event.get("text", "")
        print(f"app_mention message: {message}")

        blocks = event.get("blocks", [])

        if message.startswith("<@"):
            # Remove the mention part from the message
            message = message.split("> ", 1)[1] if "> " in message else message

            # Add file names if files are attached
            files = event.get("files", [])
            if files:
                message += "\n\nFiles attached: " + ", ".join(
                    [f["name"] for f in files]
                )

            # Remove the mention from blocks if present
            if (
                blocks
                and blocks[0].get("elements")
                and blocks[0].get("elements")[0].get("elements")
            ):
                blocks[0]["elements"][0]["elements"] = (
                    blocks[0].get("elements")[0].get("elements")[1:]
                )

        return True, message, blocks

    def _prepare_message_data(self, event: Dict) -> tuple:
        """Prepare data from a message event."""
        # Only process messages not from bots and not from app mentions
        if (
            event.get("bot_id")
            or event.get("subtype") == "bot_message"
            or event.get("text", "").startswith("<@")
        ):
            return False, "", []

        message = event.get("text", "")

        # Add file names if files are attached
        files = event.get("files", [])
        if files:
            message += "\n\nFiles attached: " + ", ".join([f["name"] for f in files])

        blocks = event.get("blocks", [])
        return True, message, blocks

    def _process_message(
        self,
        event: Dict,
        event_type: str,
        message: str,
        blocks: List,
        channel_id: str,
        client: WebClient,
        settings: Mapping,
    ) -> Response:
        """Process the message and send a response."""
        # Process files from Slack if enabled
        uploaded_files, file_info = self._process_slack_files(event, client, settings)

        # Prepare inputs for Dify
        inputs = self._prepare_dify_inputs(uploaded_files)
        print(f"inputs: {inputs}")

        # Invoke Dify app workflow
        response = self.session.app.chat.invoke(
            app_id=settings["app"]["app_id"],
            query=message,
            inputs=inputs,
            response_mode="blocking",
        )

        # Process the response
        answer = response.get("answer", "")
        if file_info:
            answer += file_info

        # Send response back to Slack
        result = self._send_slack_response(
            event, event_type, answer, blocks, channel_id, client
        )

        # Convert SlackResponse to a serializable dictionary
        result_dict = {
            "ok": result.get("ok", False),
            "channel": result.get("channel", ""),
            "ts": result.get("ts", ""),
            "message": result.get("message", {}),
        }

        return Response(
            status=200,
            response=json.dumps(result_dict),
            content_type="application/json",
        )

    def _process_slack_files(
        self, event: Dict, client: WebClient, settings: Mapping
    ) -> tuple:
        """Process files from Slack if enabled."""
        files = event.get("files", [])
        file_info = ""
        uploaded_files = []

        if settings.get("process_slack_files", False) and files:
            print("_process_slack_files: start")
            uploader = FileUploader(
                session=self.session, dify_api_key=settings.get("dify_api_key")
            )
            uploaded_files = uploader.process_slack_files(client=client, files=files)
            print("_process_slack_files: end")
            print(f"uploaded_files: {uploaded_files}")

            if uploaded_files:
                file_info = "\n\nFiles processed: " + ", ".join(
                    [f["name"] for f in uploaded_files]
                )

        return uploaded_files, file_info

    def _prepare_dify_inputs(self, uploaded_files: List) -> Dict:
        """Prepare inputs for Dify API call."""
        if not uploaded_files:
            return {}

        return {
            "files": [
                {
                    "upload_file_id": f["id"],
                    "type": f["type"],
                    "transfer_method": "local_file",
                }
                for f in uploaded_files
            ]
        }

    def _send_slack_response(
        self,
        event: Dict,
        event_type: str,
        answer: str,
        blocks: List,
        channel_id: str,
        client: WebClient,
    ) -> Dict:
        """Send response back to Slack."""
        thread_ts = event.get("thread_ts") or event.get(
            "ts"
        )  # Reply in thread if it's a thread

        # For regular messages without blocks, create a simple response
        if event_type == "message" and (
            not event.get("blocks") or len(event.get("blocks", [])) == 0
        ):
            return client.chat_postMessage(
                channel=channel_id, text=answer, thread_ts=thread_ts
            )

        # For app mentions with blocks
        if (
            blocks
            and blocks[0].get("elements")
            and blocks[0].get("elements")[0].get("elements")
        ):
            blocks[0]["elements"][0]["elements"][0]["text"] = answer

        return client.chat_postMessage(
            channel=channel_id, text=answer, blocks=blocks, thread_ts=thread_ts
        )

    def _handle_processing_error(
        self, error: Exception, event: Dict, channel_id: str, client: WebClient
    ) -> Response:
        """Handle errors during message processing."""
        err = traceback.format_exc()
        thread_ts = event.get("thread_ts") or event.get("ts")

        error_message = (
            "Sorry, I'm having trouble processing your request. Please try again later."
        )

        client.chat_postMessage(
            channel=channel_id, text=error_message + str(err), thread_ts=thread_ts
        )

        print(f"Error processing request: {error}")

        return Response(
            status=200,
            response=error_message + str(err),
            content_type="text/plain",
        )


class FileUploader:
    """
    A utility class to handle file uploads to Dify API
    """

    def __init__(
        self, session=None, dify_base_url="http://api:5001/v1", dify_api_key=None
    ):
        """
        Initialize the FileUploader

        Args:
            session: The Dify plugin session object (if available)
            dify_base_url: The base URL for Dify API (if session not available)
            dify_api_key: The API key for Dify API (if session not available)
        """
        self.session = session
        self.dify_base_url = dify_base_url
        self.dify_api_key = dify_api_key

    def upload_file_via_session(
        self, filename: str, content: bytes, mimetype: str
    ) -> Optional[Dict[str, Any]]:
        """
        Upload a file using the Dify plugin session

        Args:
            filename: The name of the file
            content: The content of the file as bytes
            mimetype: The MIME type of the file

        Returns:
            Dictionary with file information or None if upload fails
        """
        try:
            print(
                f"Uploading file via session: {filename}, mimetype: {mimetype}, content size: {len(content)} bytes"
            )

            # Use a simple test file first to verify the API is working
            test_result = self.session.file.upload(
                filename="test.txt", content=b"test content", mimetype="text/plain"
            )
            print(f"Test upload result: {test_result}")

            # Now try the actual file
            storage_file = self.session.file.upload(
                filename=filename, content=content, mimetype=mimetype
            )

            print(f"Uploaded file to Dify storage: {storage_file}")

            if storage_file:
                # Convert to app parameter format
                return storage_file.to_app_parameter()
            return None
        except Exception as e:
            print(f"Error uploading file via session: {e}")
            traceback.print_exc()
            return None

    def upload_file_via_api(
        self, filename: str, content: bytes, mimetype: str
    ) -> Optional[Dict[str, Any]]:
        """
        Upload a file using direct API calls to Dify

        Args:
            filename: The name of the file
            content: The content of the file as bytes
            mimetype: The MIME type of the file

        Returns:
            Dictionary with file information or None if upload fails
        """
        if not self.dify_base_url or not self.dify_api_key:
            print(
                "Error: dify_base_url and dify_api_key must be provided for direct API upload"
            )
            return None

        try:
            print(
                f"Uploading file via API: {filename}, mimetype: {mimetype}, content size: {len(content)} bytes"
            )

            # Prepare the file upload endpoint
            upload_url = f"{self.dify_base_url}/files/upload"
            headers = {"Authorization": f"Bearer {self.dify_api_key}"}

            # Create a temporary file to upload
            temp_file_path = f"/tmp/{filename}"
            with open(temp_file_path, "wb") as f:
                f.write(content)

            # Upload the file
            files = {"file": (filename, open(temp_file_path, "rb"), mimetype)}

            response = requests.post(upload_url, headers=headers, files=files)

            # Clean up the temporary file
            os.remove(temp_file_path)

            if response.status_code == 201 or response.status_code == 200:
                result = response.json()
                print(f"File upload API response: {result}")

                # Format the response to match Dify plugin format
                if "id" in result:
                    return {
                        "id": result["id"],
                        "name": result.get("name", filename),
                        "size": result.get("size", len(content)),
                        "extension": result.get("extension", ""),
                        "mime_type": result.get("mime_type", mimetype),
                        "type": UploadFileResponse.Type.from_mime_type(mimetype).value,
                        "url": result.get("url", ""),
                    }
            else:
                print(f"File upload API error: {response.status_code}, {response.text}")

            return None
        except Exception as e:
            print(f"Error uploading file via API: {e}")
            traceback.print_exc()
            return None

    def process_slack_files(
        self, client, files: List[Dict[str, Any]], dify_base_url=None, dify_api_key=None
    ) -> List[Dict[str, Any]]:
        """
        Process files from Slack:
        1. Download the files using the bot token
        2. Upload them to Dify storage
        3. Return the uploaded file information

        Args:
            client: The Slack WebClient
            files: List of file information from Slack
            dify_base_url: Optional Dify base URL for direct API upload
            dify_api_key: Optional Dify API key for direct API upload

        Returns:
            List of uploaded file information
        """
        uploaded_files = []

        # Set API parameters if provided
        if dify_base_url:
            self.dify_base_url = dify_base_url
        if dify_api_key:
            self.dify_api_key = dify_api_key

        for file in files:
            try:
                # Get file info
                file_name = file.get("name")
                file_type = file.get("mimetype", "application/octet-stream")
                file_url = file.get("url_private_download")

                if not file_url or not file_name:
                    print(f"Missing file URL or name: {file}")
                    continue

                # Download the file content
                headers = {"Authorization": f"Bearer {client.token}"}
                file_response = requests.get(file_url, headers=headers)

                if file_response.status_code != 200:
                    print(
                        f"Failed to download file {file_name}: {file_response.status_code}"
                    )
                    continue

                print(
                    f"Downloaded file from Slack: {file_name}, {file_type}, size: {len(file_response.content)} bytes"
                )

                # Try uploading via session first
                if self.session:
                    storage_file = self.upload_file_via_session(
                        file_name, file_response.content, file_type
                    )
                    if storage_file:
                        uploaded_files.append(storage_file)
                        continue

                # If session upload fails or session not available, try direct API upload
                if self.dify_base_url and self.dify_api_key:
                    storage_file = self.upload_file_via_api(
                        file_name, file_response.content, file_type
                    )
                    if storage_file:
                        uploaded_files.append(storage_file)

            except Exception as e:
                print(f"Error processing file {file.get('name')}: {e}")
                traceback.print_exc()

        return uploaded_files
