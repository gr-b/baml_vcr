"""
BAML VCR (Video Cassette Recorder) for capturing and replaying BAML function calls.

This module provides a decorator that uses BAML's collector functionality to:
1. Capture BAML function calls (function name, args, response) 
2. Save them to cassette files
3. Replay them in tests without making actual LLM calls

Supports both regular and streaming BAML functions.

Usage:
    from recordings.tests.utils.baml_vcr import baml_vcr
    
    @baml_vcr.use_cassette()
    def test_my_function(self):
        result = b.MyBAMLFunction(arg1="value1", arg2="value2")
        # First run: makes real LLM call and saves to cassette
        # Subsequent runs: loads from cassette without LLM call
"""

import json
import os
import hashlib
import functools
import yaml
import asyncio
from typing import Any, Dict, List, Optional, Callable, Tuple, Union
from unittest.mock import patch
from pathlib import Path
import logging
from datetime import datetime

from baml_py import Collector
import baml_client
from baml_client import b, types
try:
    from baml_client.async_client import b as async_b
except ImportError:
    async_b = None

logger = logging.getLogger(__name__)


def _serialize_for_yaml(obj: Any) -> Any:
    """Convert complex objects to YAML-serializable format."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    elif isinstance(obj, (list, tuple)):
        return [_serialize_for_yaml(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: _serialize_for_yaml(v) for k, v in obj.items()}
    elif hasattr(obj, '__dict__'):
        # Handle BAML response objects and other classes
        result = {
            '_type': obj.__class__.__name__,
            '_module': obj.__class__.__module__,
        }
        for key, value in obj.__dict__.items():
            if not key.startswith('_'):
                result[key] = _serialize_for_yaml(value)
        return result
    else:
        # Fallback to string representation
        return str(obj)


def _deserialize_from_yaml(data: Any) -> Any:
    """Reconstruct objects from YAML-serialized format."""
    if isinstance(data, dict) and '_type' in data and '_module' in data:
        # This is a serialized object
        obj_type = data['_type']
        obj_module = data['_module']
        
        # Create a simple object with the right attributes
        class ReconstructedObject:
            def __init__(self):
                self.__class__.__name__ = obj_type
                self.__class__.__module__ = obj_module
        
        obj = ReconstructedObject()
        
        # Set all attributes
        for key, value in data.items():
            if not key.startswith('_'):
                setattr(obj, key, _deserialize_from_yaml(value))
        
        return obj
    elif isinstance(data, list):
        return [_deserialize_from_yaml(item) for item in data]
    elif isinstance(data, dict):
        return {k: _deserialize_from_yaml(v) for k, v in data.items()}
    else:
        return data


class StreamingChunk:
    """Represents a single chunk in a streaming response."""
    
    def __init__(self, content: str, index: int, is_final: bool = False):
        self.content = content
        self.index = index
        self.is_final = is_final


class BamlInteraction:
    """Represents a single BAML function call interaction."""
    
    def __init__(self, function_name: str, args: Dict[str, Any], response: Any, 
                 response_type: Optional[str] = None, usage: Optional[Dict] = None,
                 is_streaming: bool = False, streaming_chunks: Optional[List[StreamingChunk]] = None,
                 final_response: Any = None):
        self.function_name = function_name
        self.args = args
        self.response = response
        self.response_type = response_type
        self.usage = usage
        self.is_streaming = is_streaming
        self.streaming_chunks = streaming_chunks or []
        self.final_response = final_response
    
    def matches(self, function_name: str, args: Dict[str, Any]) -> bool:
        """Check if this interaction matches the given function call."""
        return (self.function_name == function_name and 
                self._normalize_args(self.args) == self._normalize_args(args))
    
    def _normalize_args(self, args: Dict[str, Any]) -> str:
        """Normalize args to a comparable string representation."""
        # Convert args to a stable string representation for comparison
        serialized = _serialize_for_yaml(args)
        return json.dumps(serialized, sort_keys=True)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for YAML serialization."""
        result = {
            'function_name': self.function_name,
            'args': _serialize_for_yaml(self.args),
            'response': _serialize_for_yaml(self.response),
            'response_type': self.response_type,
            'usage': self.usage,
            'is_streaming': self.is_streaming,
        }
        
        if self.is_streaming:
            result['streaming_chunks'] = [
                {
                    'content': chunk.content,
                    'index': chunk.index,
                    'is_final': chunk.is_final
                }
                for chunk in self.streaming_chunks
            ]
            if self.final_response:
                result['final_response'] = _serialize_for_yaml(self.final_response)
        
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'BamlInteraction':
        """Create from dictionary after YAML deserialization."""
        streaming_chunks = None
        if data.get('is_streaming') and 'streaming_chunks' in data:
            streaming_chunks = [
                StreamingChunk(
                    content=chunk['content'],
                    index=chunk['index'],
                    is_final=chunk.get('is_final', False)
                )
                for chunk in data['streaming_chunks']
            ]
        
        return cls(
            function_name=data['function_name'],
            args=_deserialize_from_yaml(data['args']),
            response=_deserialize_from_yaml(data['response']),
            response_type=data.get('response_type'),
            usage=data.get('usage'),
            is_streaming=data.get('is_streaming', False),
            streaming_chunks=streaming_chunks,
            final_response=_deserialize_from_yaml(data.get('final_response')) if data.get('final_response') else None
        )


class MockStreamingResponse:
    """Mock streaming response that replays recorded chunks."""
    
    def __init__(self, chunks: List[StreamingChunk], final_response: Any, delay: float = 0.02):
        self.chunks = chunks
        self.final_response = final_response
        self.delay = delay
        self._accumulated = ""
    
    async def __aiter__(self):
        """Async iterator that yields chunks with delays."""
        for chunk in self.chunks:
            # Create a partial response object
            partial = type('Partial', (), {})()
            
            # Accumulate content
            self._accumulated += chunk.content
            
            # Create message with accumulated content
            if hasattr(self.final_response, 'message'):
                # For responses with message attribute
                partial.message = self._accumulated
            else:
                # For responses with value/state structure
                message = type('Message', (), {})()
                message.value = self._accumulated
                message.state = "Complete" if chunk.is_final else "Incomplete"
                partial.message = message
            
            yield partial
            
            # Add delay between chunks to simulate streaming
            if not chunk.is_final and self.delay > 0:
                await asyncio.sleep(self.delay)
    
    async def get_final_response(self):
        """Return the final response."""
        return self.final_response


class BamlCassette:
    """Stores and manages BAML function call recordings."""
    
    def __init__(self, cassette_path: str):
        self.cassette_path = cassette_path
        self.interactions: List[BamlInteraction] = []
        self.playback_index = 0
        self._load()
    
    def _load(self) -> None:
        """Load existing cassette if it exists."""
        # Check for both regular and streaming cassette files
        streaming_path = self.cassette_path.replace('.cassette.yaml', '.streaming.cassette.yaml')
        
        # Prioritize streaming cassette if it exists
        if os.path.exists(streaming_path):
            load_path = streaming_path
            self.cassette_path = streaming_path  # Update to point to the streaming cassette
        elif os.path.exists(self.cassette_path):
            load_path = self.cassette_path
        else:
            self.interactions = []
            return
        
        try:
            with open(load_path, 'r') as f:
                data = yaml.safe_load(f)
                if data and 'interactions' in data:
                    self.interactions = [
                        BamlInteraction.from_dict(interaction)
                        for interaction in data['interactions']
                    ]
                logger.debug(f"Loaded {len(self.interactions)} interactions from {load_path}")
        except Exception as e:
            logger.error(f"Error loading cassette {load_path}: {e}")
            self.interactions = []
    
    def save(self) -> None:
        """Save cassette to disk."""
        os.makedirs(os.path.dirname(self.cassette_path), exist_ok=True)
        
        # Check if any interactions are streaming
        has_streaming = any(interaction.is_streaming for interaction in self.interactions)
        
        # Determine the final cassette path based on streaming content
        if has_streaming and not self.cassette_path.endswith('.streaming.cassette.yaml'):
            # Replace .cassette.yaml with .streaming.cassette.yaml
            streaming_path = self.cassette_path.replace('.cassette.yaml', '.streaming.cassette.yaml')
            final_path = streaming_path
        else:
            final_path = self.cassette_path
        
        data = {
            'version': '1.0',
            'interactions': [interaction.to_dict() for interaction in self.interactions],
            'created_at': datetime.now().isoformat(),
        }
        
        with open(final_path, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            f.flush()  # Ensure content is written to disk
            os.fsync(f.fileno())  # Force OS to write to disk
        
        # Update the cassette path for future operations
        self.cassette_path = final_path
        
        logger.debug(f"Saved {len(self.interactions)} interactions to {final_path}")
    
    def record(self, interaction: BamlInteraction) -> None:
        """Record a new interaction."""
        self.interactions.append(interaction)
        logger.debug(f"Recorded interaction for {interaction.function_name}")
    
    def get_response(self, function_name: str, args: Dict[str, Any]) -> Tuple[bool, Any]:
        """
        Get a recorded response for the given function and args.
        Returns (found, response) tuple.
        """
        # Look for matching interaction starting from current playback position
        for i in range(self.playback_index, len(self.interactions)):
            interaction = self.interactions[i]
            if interaction.matches(function_name, args):
                self.playback_index = i + 1
                
                # Return appropriate response based on type
                if interaction.is_streaming:
                    # Return mock streaming response
                    return True, MockStreamingResponse(
                        interaction.streaming_chunks,
                        interaction.final_response
                    )
                else:
                    # Return regular response
                    return True, interaction.response
        
        # If not found, check from beginning (with warning)
        for i in range(0, self.playback_index):
            interaction = self.interactions[i]
            if interaction.matches(function_name, args):
                logger.warning(f"Found out-of-order match for {function_name}. Consider re-recording.")
                self.playback_index = i + 1
                
                if interaction.is_streaming:
                    return True, MockStreamingResponse(
                        interaction.streaming_chunks,
                        interaction.final_response
                    )
                else:
                    return True, interaction.response
        
        return False, None


class BamlVCR:
    """Main VCR class that provides the decorator interface."""
    
    def __init__(self):
        pass
    
    def use_cassette(self, cassette_name: Optional[str] = None, record_mode: str = "once") -> Callable:
        """
        Decorator to record/replay BAML function calls.
        
        Args:
            cassette_name: Optional cassette name. If not provided, uses test method name.
            record_mode: Recording mode - "once" (default), "new_episodes", "none", or "all"
                - "once": Record if cassette doesn't exist, otherwise playback
                - "new_episodes": Playback existing, record new calls
                - "none": Only playback, error if not found
                - "all": Always record (overwrites existing)
        """
        def decorator(test_method: Callable) -> Callable:
            @functools.wraps(test_method)
            def wrapper(test_self, *args, **kwargs):
                # Get the directory where the test file is located
                import inspect
                test_file_path = inspect.getfile(test_self.__class__)
                test_dir = os.path.dirname(test_file_path)
                cassette_dir = os.path.join(test_dir, "baml_cassettes")
                
                # Determine cassette path
                if cassette_name:
                    cassette_path = os.path.join(cassette_dir, f"{cassette_name}.cassette.yaml")
                else:
                    # Use test class and method name
                    test_class = test_self.__class__.__name__
                    test_name = test_method.__name__
                    cassette_path = os.path.join(
                        cassette_dir, 
                        f"{test_class}_{test_name}.cassette.yaml"
                    )
                
                # Create cassette
                cassette = BamlCassette(cassette_path)
                
                # Determine mode based on record_mode and cassette existence
                should_record = False
                should_playback = False
                
                if record_mode == "once":
                    if cassette.interactions:
                        should_playback = True
                    else:
                        should_record = True
                elif record_mode == "new_episodes":
                    should_playback = True
                    should_record = True
                elif record_mode == "none":
                    should_playback = True
                elif record_mode == "all":
                    should_record = True
                    cassette.interactions = []  # Clear existing
                
                # Create a context manager for this cassette session
                vcr_context = VCRContext(
                    cassette=cassette,
                    should_record=should_record,
                    should_playback=should_playback,
                    record_mode=record_mode
                )
                
                try:
                    if should_playback:
                        logger.info(f"BAML VCR: Playback mode for {cassette_path}")
                    if should_record:
                        logger.info(f"BAML VCR: Record mode for {cassette_path}")
                    
                    # Setup interception
                    vcr_context.setup()
                    
                    # Run the test
                    result = test_method(test_self, *args, **kwargs)
                    
                    return result
                finally:
                    # Save if we recorded anything
                    logger.debug("About to call finalize")
                    vcr_context.finalize()
                    logger.debug("Finalize completed")
                    
                    # Cleanup
                    vcr_context.cleanup()
            
            return wrapper
        return decorator


class StreamingRecorder:
    """Helper to record streaming responses."""
    
    def __init__(self, original_stream):
        self.original_stream = original_stream
        self.chunks = []
        self.final_response = None
        self._accumulated = ""
    
    async def __aiter__(self):
        """Record chunks as they stream."""
        chunk_index = 0
        
        async for partial in self.original_stream:
            # Yield the partial as-is
            yield partial
            
            # Record the chunk
            if hasattr(partial, 'message') and partial.message is not None:
                current_message = partial.message
                
                # Extract new content
                if isinstance(current_message, str):
                    new_content = current_message[len(self._accumulated):]
                    self._accumulated = current_message
                    is_final = False
                elif hasattr(current_message, 'value'):
                    new_content = current_message.value[len(self._accumulated):]
                    self._accumulated = current_message.value
                    is_final = hasattr(current_message, 'state') and current_message.state == "Complete"
                else:
                    continue
                
                if new_content:
                    self.chunks.append(StreamingChunk(
                        content=new_content,
                        index=chunk_index,
                        is_final=is_final
                    ))
                    chunk_index += 1
    
    async def get_final_response(self):
        """Get and record the final response."""
        if self.final_response is None:
            self.final_response = await self.original_stream.get_final_response()
        return self.final_response


class VCRContext:
    """Context manager for a single VCR session."""
    
    def __init__(self, cassette: BamlCassette, should_record: bool, 
                 should_playback: bool, record_mode: str):
        self.cassette = cassette
        self.should_record = should_record
        self.should_playback = should_playback
        self.record_mode = record_mode
        self.collector: Optional[Collector] = None
        self.patches: List[Any] = []
        self.intercepted_calls: List[Tuple[str, Dict, Any]] = []
        self.streaming_responses: Dict[int, StreamingRecorder] = {}
    
    def setup(self) -> None:
        """Setup BAML call interception."""
        # Get all BAML function names from b and b.stream
        baml_functions = []
        
        # Regular sync functions
        for name in dir(b):
            if not name.startswith('_') and callable(getattr(b, name, None)):
                baml_functions.append((b, name, False, False))
        
        # Sync streaming functions
        if hasattr(b, 'stream'):
            for name in dir(b.stream):
                if not name.startswith('_') and callable(getattr(b.stream, name, None)):
                    baml_functions.append((b.stream, name, True, False))
        
        # Async functions if available
        if async_b:
            # Regular async functions
            for name in dir(async_b):
                if not name.startswith('_') and callable(getattr(async_b, name, None)):
                    baml_functions.append((async_b, name, False, True))
            
            # Async streaming functions
            if hasattr(async_b, 'stream'):
                for name in dir(async_b.stream):
                    if not name.startswith('_') and callable(getattr(async_b.stream, name, None)):
                        baml_functions.append((async_b.stream, name, True, True))
        
        logger.debug(f"BAML VCR: Found {len(baml_functions)} functions to patch")
        
        # Create collector if recording
        if self.should_record:
            self.collector = Collector(name="baml_vcr")
        
        # Patch each function
        for obj, func_name, is_streaming, is_async in baml_functions:
            original_func = getattr(obj, func_name)
            if not callable(original_func):
                continue
            
            # Create wrapper that captures self context
            wrapper = self._create_wrapper(func_name, original_func, is_streaming, is_async)
            
            # Create and apply patch
            patch_obj = patch.object(obj, func_name, wrapper)
            patch_obj.start()
            self.patches.append(patch_obj)
    
    def _create_wrapper(self, func_name: str, original_func: Callable, is_streaming: bool, is_async: bool) -> Callable:
        """Create a wrapper function for the given BAML function."""
        vcr_context = self  # Capture self in closure
        
        if is_async:
            if is_streaming:
                # For async streaming, the original function returns an async iterator, not a coroutine
                # We need to maintain the async iterator interface
                def async_streaming_wrapper(**kwargs):
                    return vcr_context._handle_call_sync(func_name, original_func, is_streaming, kwargs)
                return async_streaming_wrapper
            else:
                # For non-streaming async, use async wrapper
                async def async_wrapper(**kwargs):
                    return await vcr_context._handle_call_async(func_name, original_func, is_streaming, kwargs)
                return async_wrapper
        else:
            def sync_wrapper(**kwargs):
                return vcr_context._handle_call_sync(func_name, original_func, is_streaming, kwargs)
            return sync_wrapper
    
    def _handle_call_sync(self, func_name: str, original_func: Callable, is_streaming: bool, kwargs: Dict) -> Any:
        """Handle a BAML function call (sync version)."""
        # Extract baml_options
        baml_options = kwargs.get('baml_options', {})
        
        # Check if we have a recorded response
        if self.should_playback and self.cassette:
            # Remove baml_options for comparison
            compare_kwargs = kwargs.copy()
            compare_kwargs.pop('baml_options', None)
            
            logger.debug(f"BAML VCR: Looking for playback of {func_name} with {len(self.cassette.interactions)} interactions")
            found, response = self.cassette.get_response(func_name, compare_kwargs)
            if found:
                logger.info(f"BAML VCR: Playing back {func_name}")
                return response
            else:
                logger.warning(f"BAML VCR: No matching interaction found for {func_name}")
        
        # If recording, add collector
        if self.should_record and self.collector:
            if isinstance(baml_options, dict):
                baml_options = baml_options.copy()
                baml_options['collector'] = self.collector
                kwargs['baml_options'] = baml_options
        
        # If playback mode and no recording, we must have a cassette entry
        if self.should_playback and not self.should_record:
            # We already checked for a cassette entry above
            # If we're here, we don't have one and shouldn't make a real call
            if self.record_mode == "none":
                # Get comparison kwargs for error message
                compare_kwargs = kwargs.copy()
                compare_kwargs.pop('baml_options', None)
                raise ValueError(
                    f"No recorded response found for {func_name} with args {compare_kwargs} "
                    f"and record_mode is 'none'. Delete the cassette or change record_mode to re-record."
                )
        
        # Make the real call
        try:
            result = original_func(**kwargs)
            
            # Handle recording based on response type
            if self.should_record:
                # Remove baml_options from saved kwargs
                saved_kwargs = kwargs.copy()
                saved_kwargs.pop('baml_options', None)
                
                if is_streaming:
                    # Wrap streaming response with recorder
                    recorder = StreamingRecorder(result)
                    response_id = id(recorder)
                    self.streaming_responses[response_id] = recorder
                    
                    # Store info for later saving
                    self.intercepted_calls.append((func_name, saved_kwargs, recorder))
                    logger.debug(f"BAML VCR: Intercepted streaming call to {func_name}")
                    
                    return recorder
                else:
                    # Regular response
                    self.intercepted_calls.append((func_name, saved_kwargs, result))
                    logger.debug(f"BAML VCR: Intercepted call to {func_name}")
            
            return result
        except Exception as e:
            raise
    
    async def _handle_call_async(self, func_name: str, original_func: Callable, is_streaming: bool, kwargs: Dict) -> Any:
        """Handle a BAML function call (async version)."""
        # Extract baml_options
        baml_options = kwargs.get('baml_options', {})
        
        # Check if we have a recorded response
        if self.should_playback and self.cassette:
            # Remove baml_options for comparison
            compare_kwargs = kwargs.copy()
            compare_kwargs.pop('baml_options', None)
            
            found, response = self.cassette.get_response(func_name, compare_kwargs)
            if found:
                logger.debug(f"BAML VCR: Playing back {func_name}")
                return response
        
        # If recording, add collector
        if self.should_record and self.collector:
            if isinstance(baml_options, dict):
                baml_options = baml_options.copy()
                baml_options['collector'] = self.collector
                kwargs['baml_options'] = baml_options
        
        # If playback mode and no recording, we must have a cassette entry
        if self.should_playback and not self.should_record:
            # We already checked for a cassette entry above
            # If we're here, we don't have one and shouldn't make a real call
            if self.record_mode == "none":
                # Get comparison kwargs for error message
                compare_kwargs = kwargs.copy()
                compare_kwargs.pop('baml_options', None)
                raise ValueError(
                    f"No recorded response found for {func_name} with args {compare_kwargs} "
                    f"and record_mode is 'none'. Delete the cassette or change record_mode to re-record."
                )
        
        # Make the real call
        try:
            result = await original_func(**kwargs)
            
            # Handle recording based on response type
            if self.should_record:
                # Remove baml_options from saved kwargs
                saved_kwargs = kwargs.copy()
                saved_kwargs.pop('baml_options', None)
                
                if is_streaming:
                    # Wrap streaming response with recorder
                    recorder = StreamingRecorder(result)
                    response_id = id(recorder)
                    self.streaming_responses[response_id] = recorder
                    
                    # Store info for later saving
                    self.intercepted_calls.append((func_name, saved_kwargs, recorder))
                    logger.debug(f"BAML VCR: Intercepted streaming call to {func_name}")
                    
                    return recorder
                else:
                    # Regular response
                    self.intercepted_calls.append((func_name, saved_kwargs, result))
                    logger.debug(f"BAML VCR: Intercepted call to {func_name}")
            
            return result
        except Exception as e:
            raise
    
    def finalize(self) -> None:
        """Save recorded interactions if any."""
        logger.debug(f"BAML VCR: Finalize called, should_record={self.should_record}")
        if self.should_record:
            logger.info(f"BAML VCR: Intercepted {len(self.intercepted_calls)} calls")
            if self.intercepted_calls:
                # Simply run the save synchronously in the finalize method
                # Convert intercepted calls to BamlInteraction objects
                for func_name, kwargs, response in self.intercepted_calls:
                    # Check if this is a streaming response
                    if isinstance(response, StreamingRecorder):
                        # For streaming responses, we might not have the final response yet
                        # We'll save without it for now and record the chunks
                        final_response = None
                        
                        interaction = BamlInteraction(
                            function_name=func_name,
                            args=kwargs,
                            response=None,  # No single response for streaming
                            response_type=None,
                            usage=self._get_usage_for_function(func_name),
                            is_streaming=True,
                            streaming_chunks=response.chunks,
                            final_response=final_response
                        )
                    else:
                        # Regular response
                        response_type = f"{response.__class__.__module__}.{response.__class__.__name__}" if response is not None else None
                        
                        interaction = BamlInteraction(
                            function_name=func_name,
                            args=kwargs,
                            response=response,
                            response_type=response_type,
                            usage=self._get_usage_for_function(func_name),
                            is_streaming=False
                        )
                    
                    self.cassette.record(interaction)
                
                # Save to disk
                self.cassette.save()
                logger.info(f"BAML VCR: Saved {len(self.intercepted_calls)} interactions")
                
                # Verify file exists
                import time
                time.sleep(0.1)  # Give filesystem a moment
                if os.path.exists(self.cassette.cassette_path):
                    logger.debug(f"BAML VCR: Confirmed file exists at {self.cassette.cassette_path}")
                else:
                    logger.error(f"BAML VCR: File NOT found at {self.cassette.cassette_path}")
                    logger.error(f"Current working directory: {os.getcwd()}")
                    logger.error(f"Absolute path: {os.path.abspath(self.cassette.cassette_path)}")
            else:
                logger.warning("BAML VCR: No calls intercepted to save")
    
    def _run_save_in_new_loop(self):
        """Run save in a new event loop in a separate thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._save_intercepted_calls())
            # Return a result to confirm completion
            return True
        finally:
            loop.close()
    
    async def _save_intercepted_calls(self) -> None:
        """Save intercepted calls to cassette."""
        logger.debug(f"_save_intercepted_calls called with {len(self.intercepted_calls)} calls")
        # Convert intercepted calls to BamlInteraction objects
        for func_name, kwargs, response in self.intercepted_calls:
            # Check if this is a streaming response
            if isinstance(response, StreamingRecorder):
                # Get the final response if not already retrieved
                try:
                    final_response = await response.get_final_response()
                except:
                    final_response = None
                
                interaction = BamlInteraction(
                    function_name=func_name,
                    args=kwargs,
                    response=None,  # No single response for streaming
                    response_type=None,
                    usage=self._get_usage_for_function(func_name),
                    is_streaming=True,
                    streaming_chunks=response.chunks,
                    final_response=final_response
                )
            else:
                # Regular response
                response_type = f"{response.__class__.__module__}.{response.__class__.__name__}" if response is not None else None
                
                interaction = BamlInteraction(
                    function_name=func_name,
                    args=kwargs,
                    response=response,
                    response_type=response_type,
                    usage=self._get_usage_for_function(func_name),
                    is_streaming=False
                )
            
            self.cassette.record(interaction)
        
        # Save to disk
        self.cassette.save()
        logger.info(f"BAML VCR: Saved {len(self.intercepted_calls)} interactions")
    
    def _get_usage_for_function(self, func_name: str) -> Optional[Dict]:
        """Get usage stats from collector for a function."""
        if self.collector and self.collector.logs:
            # Find the log for this function call
            for log in self.collector.logs:
                if log.function_name == func_name:
                    if hasattr(log, 'usage') and log.usage:
                        return {
                            'input_tokens': getattr(log.usage, 'input_tokens', None),
                            'output_tokens': getattr(log.usage, 'output_tokens', None)
                        }
                    break
        return None
    
    def cleanup(self) -> None:
        """Clean up patches and state."""
        # Stop all patches
        for patch_obj in self.patches:
            patch_obj.stop()
        
        self.patches.clear()
        self.intercepted_calls.clear()
        self.streaming_responses.clear()
        self.collector = None


# Global instance
baml_vcr = BamlVCR()
