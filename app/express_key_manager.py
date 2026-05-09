import random
from typing import List, Optional, Tuple
import config as app_config


class ExpressKeyManager:
    """
    Manager for Vertex Express API keys with support for both random and round-robin selection strategies.
    Similar to CredentialManager but specifically for Express API keys.
    """
    
    def __init__(self):
        """Initialize the Express Key Manager with API keys from config."""
        self.express_keys: List[str] = app_config.VERTEX_EXPRESS_API_KEY_VAL
        self.round_robin_index: int = 0
        
    def get_total_keys(self) -> int:
        """Get the total number of available Express API keys."""
        return len(self.express_keys)
    
    def get_random_express_key(self) -> Optional[Tuple[int, str]]:
        """
        Get a random Express API key.
        Returns (original_index, key) tuple or None if no keys available.
        """
        if not self.express_keys:
            print("WARNING: No Express API keys available for selection.")
            return None
            
        print(f"DEBUG: Using random Express API key selection strategy.")
        
        # Create list of indexed keys
        indexed_keys = list(enumerate(self.express_keys))
        # Shuffle to randomize order
        random.shuffle(indexed_keys)
        
        # Return the first key (which is random due to shuffle)
        original_idx, key = indexed_keys[0]
        return (original_idx, key)
    
    def get_roundrobin_express_key(self) -> Optional[Tuple[int, str]]:
        """
        Get an Express API key using round-robin selection.
        Returns (original_index, key) tuple or None if no keys available.
        """
        if not self.express_keys:
            print("WARNING: No Express API keys available for selection.")
            return None
            
        print(f"DEBUG: Using round-robin Express API key selection strategy.")
        
        # Ensure round_robin_index is within bounds
        if self.round_robin_index >= len(self.express_keys):
            self.round_robin_index = 0
            
        # Get the key at current index
        key = self.express_keys[self.round_robin_index]
        original_idx = self.round_robin_index
        
        # Move to next index for next call
        self.round_robin_index = (self.round_robin_index + 1) % len(self.express_keys)
        
        return (original_idx, key)
    
    def get_express_api_key(self) -> Optional[Tuple[int, str]]:
        """
        Get an Express API key based on the configured selection strategy.
        Checks ROUNDROBIN config and calls the appropriate method.
        Returns (original_index, key) tuple or None if no keys available.
        """
        if app_config.ROUNDROBIN:
            return self.get_roundrobin_express_key()
        else:
            return self.get_random_express_key()
    
    def get_all_keys_indexed(self) -> List[Tuple[int, str]]:
        """
        Get all Express API keys with their indices.
        Useful for retry logic where we need to try all keys.
        Returns list of (original_index, key) tuples.
        """
        return list(enumerate(self.express_keys))
    
    def refresh_keys(self):
        """
        Refresh the Express API keys from config.
        This allows for dynamic updates if the config is reloaded.
        """
        self.express_keys = app_config.VERTEX_EXPRESS_API_KEY_VAL
        # Reset round-robin index if keys changed
        if self.round_robin_index >= len(self.express_keys):
            self.round_robin_index = 0
        print(f"INFO: Express API keys refreshed. Total keys: {self.get_total_keys()}")