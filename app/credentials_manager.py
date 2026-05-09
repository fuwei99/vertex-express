import os
import glob
import random
import json
from typing import List, Dict, Any
from google.auth.transport.requests import Request as AuthRequest
from google.oauth2 import service_account
import config as app_config # Changed from relative

# Helper function to parse multiple JSONs from a string
def parse_multiple_json_credentials(json_str: str) -> List[Dict[str, Any]]:
    """
    Parse multiple JSON objects from a string separated by commas.
    Format expected: {json_object1},{json_object2},...
    Returns a list of parsed JSON objects.
    """
    credentials_list = []
    nesting_level = 0
    current_object_start = -1
    str_length = len(json_str)

    for i, char in enumerate(json_str):
        if char == '{':
            if nesting_level == 0:
                current_object_start = i
            nesting_level += 1
        elif char == '}':
            if nesting_level > 0:
                nesting_level -= 1
                if nesting_level == 0 and current_object_start != -1:
                    # Found a complete top-level JSON object
                    json_object_str = json_str[current_object_start : i + 1]
                    try:
                        credentials_info = json.loads(json_object_str)
                        # Basic validation for service account structure
                        required_fields = ["type", "project_id", "private_key_id", "private_key", "client_email"]
                        if all(field in credentials_info for field in required_fields):
                             credentials_list.append(credentials_info)
                             print(f"DEBUG: Successfully parsed a JSON credential object.")
                        else:
                             print(f"WARNING: Parsed JSON object missing required fields: {json_object_str[:100]}...")
                    except json.JSONDecodeError as e:
                        print(f"ERROR: Failed to parse JSON object segment: {json_object_str[:100]}... Error: {e}")
                    current_object_start = -1 # Reset for the next object
            else:
                # Found a closing brace without a matching open brace in scope, might indicate malformed input
                 print(f"WARNING: Encountered unexpected '}}' at index {i}. Input might be malformed.")


    if nesting_level != 0:
        print(f"WARNING: JSON string parsing ended with non-zero nesting level ({nesting_level}). Check for unbalanced braces.")

    print(f"DEBUG: Parsed {len(credentials_list)} credential objects from the input string.")
    return credentials_list
def _refresh_auth(credentials):
    """Helper function to refresh GCP token."""
    if not credentials:
        print("ERROR: _refresh_auth called with no credentials.")
        return None
    try:
        # Assuming credentials object has a project_id attribute for logging
        project_id_for_log = getattr(credentials, 'project_id', 'Unknown')
        print(f"INFO: Attempting to refresh token for project: {project_id_for_log}...")
        credentials.refresh(AuthRequest())
        print(f"INFO: Token refreshed successfully for project: {project_id_for_log}")
        return credentials.token
    except Exception as e:
        project_id_for_log = getattr(credentials, 'project_id', 'Unknown')
        print(f"ERROR: Error refreshing GCP token for project {project_id_for_log}: {e}")
        return None


# Credential Manager for handling multiple service accounts
class CredentialManager:
    def __init__(self): # default_credentials_dir is now handled by config
        # Use CREDENTIALS_DIR from config
        self.credentials_dir = app_config.CREDENTIALS_DIR
        self.credentials_files = []
        self.current_index = 0
        self.credentials = None
        self.project_id = None
        # New: Store credentials loaded directly from JSON objects
        self.in_memory_credentials: List[Dict[str, Any]] = []
        # Round-robin index for tracking position
        self.round_robin_index = 0
        self.load_credentials_list() # Load file-based credentials initially

    def add_credential_from_json(self, credentials_info: Dict[str, Any]) -> bool:
        """
        Add a credential from a JSON object to the manager's in-memory list.

        Args:
            credentials_info: Dict containing service account credentials

        Returns:
            bool: True if credential was added successfully, False otherwise
        """
        try:
            # Validate structure again before creating credentials object
            required_fields = ["type", "project_id", "private_key_id", "private_key", "client_email"]
            if not all(field in credentials_info for field in required_fields):
                 print(f"WARNING: Skipping JSON credential due to missing required fields.")
                 return False

            credentials = service_account.Credentials.from_service_account_info(
                credentials_info,
                scopes=['https://www.googleapis.com/auth/cloud-platform']
            )
            project_id = credentials.project_id
            print(f"DEBUG: Successfully created credentials object from JSON for project: {project_id}")

            # Store the credentials object and project ID
            self.in_memory_credentials.append({
                'credentials': credentials,
                'project_id': project_id,
                 'source': 'json_string' # Add source for clarity
            })
            print(f"INFO: Added credential for project {project_id} from JSON string to Credential Manager.")
            return True
        except Exception as e:
            print(f"ERROR: Failed to create credentials from parsed JSON object: {e}")
            return False

    def load_credentials_from_json_list(self, json_list: List[Dict[str, Any]]) -> int:
        """
        Load multiple credentials from a list of JSON objects into memory.

        Args:
            json_list: List of dicts containing service account credentials

        Returns:
            int: Number of credentials successfully loaded
        """
        # Avoid duplicates if called multiple times
        existing_projects = {cred['project_id'] for cred in self.in_memory_credentials}
        success_count = 0
        newly_added_projects = set()

        for credentials_info in json_list:
             project_id = credentials_info.get('project_id')
             # Check if this project_id from JSON exists in files OR already added from JSON
             is_duplicate_file = any(os.path.basename(f) == f"{project_id}.json" for f in self.credentials_files) # Basic check
             is_duplicate_mem = project_id in existing_projects or project_id in newly_added_projects

             if project_id and not is_duplicate_file and not is_duplicate_mem:
                 if self.add_credential_from_json(credentials_info):
                     success_count += 1
                     newly_added_projects.add(project_id)
             elif project_id:
                  print(f"DEBUG: Skipping duplicate credential for project {project_id} from JSON list.")


        if success_count > 0:
             print(f"INFO: Loaded {success_count} new credentials from JSON list into memory.")
        return success_count

    def load_credentials_list(self):
        """Load the list of available credential files"""
        # Look for all .json files in the credentials directory
        pattern = os.path.join(self.credentials_dir, "*.json")
        self.credentials_files = glob.glob(pattern)

        if not self.credentials_files:
            # print(f"No credential files found in {self.credentials_dir}")
            pass # Don't return False yet, might have in-memory creds
        else:
             print(f"Found {len(self.credentials_files)} credential files: {[os.path.basename(f) for f in self.credentials_files]}")

        # Check total credentials
        return self.get_total_credentials() > 0

    def refresh_credentials_list(self):
        """Refresh the list of credential files and return if any credentials exist"""
        old_file_count = len(self.credentials_files)
        self.load_credentials_list() # Reloads file list
        new_file_count = len(self.credentials_files)

        if old_file_count != new_file_count:
            print(f"Credential files updated: {old_file_count} -> {new_file_count}")

        # Total credentials = files + in-memory
        total_credentials = self.get_total_credentials()
        print(f"DEBUG: Refresh check - Total credentials available: {total_credentials}")
        return total_credentials > 0

    def get_total_credentials(self):
        """Returns the total number of credentials (file + in-memory)."""
        return len(self.credentials_files) + len(self.in_memory_credentials)


    def _get_all_credential_sources(self):
        """
        Get all available credential sources (files and in-memory).
        Returns a list of dicts with 'type' and 'value' keys.
        """
        all_sources = []
        
        # Add file paths (as type 'file')
        for file_path in self.credentials_files:
            all_sources.append({'type': 'file', 'value': file_path})
        
        # Add in-memory credentials (as type 'memory_object')
        for idx, mem_cred_info in enumerate(self.in_memory_credentials):
            all_sources.append({'type': 'memory_object', 'value': mem_cred_info, 'original_index': idx})
        
        return all_sources

    def _load_credential_from_source(self, source_info):
        """
        Load a credential from a given source.
        Returns (credentials, project_id) tuple or (None, None) on failure.
        """
        source_type = source_info['type']
        
        if source_type == 'file':
            file_path = source_info['value']
            print(f"DEBUG: Attempting to load credential from file: {os.path.basename(file_path)}")
            try:
                credentials = service_account.Credentials.from_service_account_file(
                    file_path,
                    scopes=['https://www.googleapis.com/auth/cloud-platform']
                )
                project_id = credentials.project_id
                print(f"INFO: Successfully loaded credential from file {os.path.basename(file_path)} for project: {project_id}")
                self.credentials = credentials  # Cache last successfully loaded
                self.project_id = project_id
                return credentials, project_id
            except Exception as e:
                print(f"ERROR: Failed loading credentials file {os.path.basename(file_path)}: {e}")
                return None, None
        
        elif source_type == 'memory_object':
            mem_cred_detail = source_info['value']
            credentials = mem_cred_detail.get('credentials')
            project_id = mem_cred_detail.get('project_id')
            
            if credentials and project_id:
                print(f"INFO: Using in-memory credential for project: {project_id} (Source: {mem_cred_detail.get('source', 'unknown')})")
                self.credentials = credentials  # Cache last successfully loaded/used
                self.project_id = project_id
                return credentials, project_id
            else:
                print(f"WARNING: In-memory credential entry missing 'credentials' or 'project_id' at original index {source_info.get('original_index', 'N/A')}.")
                return None, None
        
        return None, None

    def get_random_credentials(self):
        """
        Get a random credential from available sources.
        Tries each available credential source at most once in random order.
        Returns (credentials, project_id) tuple or (None, None) if all fail.
        """
        all_sources = self._get_all_credential_sources()
        
        if not all_sources:
            print("WARNING: No credentials available for selection (no files or in-memory).")
            return None, None
        
        print(f"DEBUG: Using random credential selection strategy.")
        sources_to_try = all_sources.copy()
        random.shuffle(sources_to_try)  # Shuffle to try in a random order
        
        for source_info in sources_to_try:
            credentials, project_id = self._load_credential_from_source(source_info)
            if credentials and project_id:
                return credentials, project_id
        
        print("WARNING: All available credential sources failed to load.")
        return None, None

    def get_roundrobin_credentials(self):
        """
        Get a credential using round-robin selection.
        Tries credentials in order, cycling through all available sources.
        Returns (credentials, project_id) tuple or (None, None) if all fail.
        """
        all_sources = self._get_all_credential_sources()
        
        if not all_sources:
            print("WARNING: No credentials available for selection (no files or in-memory).")
            return None, None
        
        print(f"DEBUG: Using round-robin credential selection strategy.")
        
        # Ensure round_robin_index is within bounds
        if self.round_robin_index >= len(all_sources):
            self.round_robin_index = 0
        
        # Create ordered list starting from round_robin_index
        ordered_sources = all_sources[self.round_robin_index:] + all_sources[:self.round_robin_index]
        
        # Move to next index for next call
        self.round_robin_index = (self.round_robin_index + 1) % len(all_sources)
        
        # Try credentials in round-robin order
        for source_info in ordered_sources:
            credentials, project_id = self._load_credential_from_source(source_info)
            if credentials and project_id:
                return credentials, project_id
        
        print("WARNING: All available credential sources failed to load.")
        return None, None

    def get_credentials(self):
        """
        Get credentials based on the configured selection strategy.
        Checks ROUNDROBIN config and calls the appropriate method.
        Returns (credentials, project_id) tuple or (None, None) if all fail.
        """
        if app_config.ROUNDROBIN:
            return self.get_roundrobin_credentials()
        else:
            return self.get_random_credentials()