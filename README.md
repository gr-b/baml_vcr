# BAML VCR - Cache LLM calls in tests in 1 line

```python
from baml_vcr import baml_vcr
import baml_client
from baml_client import b

class TestMyBAMLFunctions:

    @baml_vcr.use_cassette() # <--- All you need
    def test_simple_function(self):
        # First run: makes real LLM call and saves to cassette
        result = b.MyBAMLFunction(arg1="value1", arg2="value2")
        assert result.success
        
        # Subsequent runs: loads from cassette without LLM call
```

A recording and playback system for BAML function calls, inspired by the VCR pattern in testing. BAML VCR allows you to capture LLM interactions during test runs and replay them without making actual API calls, making your tests faster, more reliable, and cost-effective.

Source: [https://github.com/BoundaryML/baml/tree/canary](https://github.com/BoundaryML/baml/tree/canary)

## Features

- **Record & Replay**: Capture BAML function calls and their responses, then replay them in subsequent test runs
- **Streaming Support**: Full support for streaming BAML functions with chunk-by-chunk recording
- **Multiple Recording Modes**: Choose between "once", "new_episodes", "none", or "all" recording strategies
- **Async Support**: Works with both synchronous and asynchronous BAML functions
- **YAML Storage**: Human-readable cassette files stored in YAML format
- **Automatic Test Discovery**: Cassettes are automatically named based on test class and method names
- **Type Preservation**: Preserves complex BAML response types during serialization

## Installation

BAML VCR is not yet available on PyPI. To install, clone the repository and install from source:

```bash
git clone https://github.com/gr-b/baml_vcr.git
cd baml_vcr
pip install -e .
```

Or install directly from GitHub:

```bash
pip install git+https://github.com/gr-b/baml_vcr.git
```



## Recording Modes

### `once` (default)
- Records interactions if cassette doesn't exist
- Replays from cassette if it exists
- Perfect for standard test scenarios

### `new_episodes`
- Replays existing interactions
- Records any new, unmatched calls
- Useful when adding new test cases

### `none`
- Only replays, never records
- Raises error if interaction not found
- Use in CI/CD pipelines

### `all`
- Always records, overwrites existing cassette
- Useful for refreshing test data

## Advanced Usage

### Custom Cassette Names

```python
@baml_vcr.use_cassette(cassette_name="custom_test_name")
def test_with_custom_name(self):
    result = b.MyFunction(input="test")
```

### Different Recording Modes

```python
@baml_vcr.use_cassette(record_mode="new_episodes")
def test_incremental_recording(self):
    # Existing calls are replayed
    result1 = b.Function1(input="test")
    
    # New calls are recorded
    result2 = b.Function2(input="new test")
```

### Streaming Functions

```python
@baml_vcr.use_cassette()
async def test_streaming_function(self):
    stream = b.stream.StreamingFunction(prompt="Generate a story")
    
    # First run: records each chunk
    async for chunk in stream:
        print(chunk.message)
    
    final = await stream.get_final_response()
    # Subsequent runs: replays chunks with realistic timing
```

## Cassette File Structure

Cassettes are stored in `baml_cassettes/` directory next to your test files:

```
tests/
├── test_my_functions.py
└── baml_cassettes/
    ├── TestClass_test_method.cassette.yaml
    └── TestClass_test_streaming.streaming.cassette.yaml
```

### Example Cassette Content

```yaml
version: '1.0'
interactions:
- function_name: ExtractUserInfo
  args:
    text: "John Doe, 30 years old, john@example.com"
  response:
    _type: UserInfo
    _module: baml_client.types
    name: John Doe
    age: 30
    email: john@example.com
  response_type: baml_client.types.UserInfo
  usage:
    input_tokens: 15
    output_tokens: 12
  is_streaming: false
created_at: '2024-01-15T10:30:00.123456'
```

## How It Works

1. **Interception**: BAML VCR patches BAML client functions at runtime
2. **Recording**: When recording is enabled, it uses BAML's Collector to capture function calls and responses
3. **Storage**: Interactions are serialized to YAML, preserving type information
4. **Playback**: On replay, responses are returned from the cassette without making API calls
5. **Streaming**: For streaming functions, individual chunks are recorded and replayed with realistic timing

## Best Practices

1. **Commit Cassettes**: Include cassette files in version control for consistent test behavior
2. **Refresh Periodically**: Use `record_mode="all"` occasionally to update test data
3. **Separate Test Data**: Use different cassettes for different test scenarios
4. **Review Changes**: Check cassette diffs when updating to ensure expected behavior
5. **CI/CD**: Use `record_mode="none"` in CI to ensure deterministic tests

## Troubleshooting

### No Cassette Found
If you see "No recorded response found", either:
- Delete the cassette to re-record
- Change `record_mode` to "once" or "all"
- Check that the function arguments match exactly

### Streaming Issues
Streaming cassettes are saved with `.streaming.cassette.yaml` extension. Ensure you're not mixing streaming and non-streaming calls in the same test.

### Type Errors
BAML VCR preserves type information during serialization. If you encounter type errors, check that your BAML client version matches between recording and playback.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request. If it does not break existing functionality, I will merge it.

## License

MIT License - see LICENSE file for details
