"""Configurable local/cloud AI tests; network traffic stays on loopback."""
from contextlib import redirect_stdout
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch

import ai_review as ai
import main as app


class CompatibleTests(unittest.TestCase):
    def setUp(self):
        self.config = ai.ReviewConfig('shadow', 'fixture-key', 'fixture-model', 5, 6, 'http://127.0.0.1:12345', 'compatible')
        self.payload = {'state': {'files': [{'name': 'Artist - Song.mp3'}]},
                        'questions': {'file_0': ai.choice('Version?', {'ordinary': 'Studio', 'special': 'Live'})}}

    def test_endpoint_forms_and_invalid_credentials(self):
        for value in ('http://localhost:11434', 'http://localhost:11434/v1/', 'http://localhost:11434/v1/chat/completions'):
            self.assertEqual(ai.endpoint_url(value), 'http://localhost:11434/v1/chat/completions')
        self.assertEqual(ai.endpoint_url('https://cloud.invalid/custom/v1'), 'https://cloud.invalid/custom/v1/chat/completions')
        for value in ('file:///tmp/model', 'http://key:secret@localhost/v1', 'https://host.invalid/#secret', ''):
            with self.subTest(value=value), self.assertRaises(ValueError): ai.endpoint_url(value)

    def test_new_settings_take_precedence_over_legacy_service(self):
        values = {'AI_API_URL': 'http://localhost:1234/v1', 'AI_MODEL': 'local-model', 'AI_API_KEY': '', 'TYPESAFE_MODE': 'invalid'}
        with patch.dict(os.environ, values, clear=True):
            config = ai.ReviewConfig.load()
        self.assertEqual(config.provider, 'compatible')
        self.assertEqual(config.model, 'local-model')
        self.assertEqual(config.api_key, '')

    def test_off_ignores_unused_configuration_and_invalid_limits_fail(self):
        with patch.dict(os.environ, {'AI_API_URL': 'invalid', 'AI_MODE': 'off', 'AI_TIMEOUT': 'invalid'}, clear=True):
            self.assertEqual(ai.ReviewConfig.load().mode, 'off')
        for setting in ({'AI_TIMEOUT': 'nan'}, {'AI_MAX_CALLS': '0'}, {'AI_MAX_TOKENS': '12'}, {'AI_MODEL': ''}):
            with patch.dict(os.environ, {'AI_API_URL': 'http://localhost:1234', 'AI_MODEL': 'local', **setting}, clear=True):
                with self.assertRaises(ValueError): ai.ReviewConfig.load()

    def mock_response(self, content, status=200):
        connection = Mock()
        connection.getresponse.return_value.status = status
        connection.getresponse.return_value.read.return_value = json.dumps({'model': 'resolved-local', 'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}).encode()
        return connection

    def test_http_structured_review_and_optional_key(self):
        connection = self.mock_response('```json\n{"answers":{"file_0":{"choice":"ordinary","confidence":0.8}}}\n```')
        with patch.object(ai.http.client, 'HTTPConnection', return_value=connection):
            result = ai.request_review(self.payload, replace(self.config, api_key=''))
        ai.validate_response(result, self.payload['questions'])
        self.assertEqual(result['model'], 'resolved-local')
        self.assertEqual(result['usage'], {'input_tokens': 10, 'output_tokens': 5})
        self.assertNotIn('Authorization', connection.request.call_args.kwargs['headers'])
        self.assertEqual(result['provider']['choice_encoding'], 'one_hot')

    def test_https_uses_bearer_key_and_never_follows_redirects(self):
        connection = self.mock_response('', status=302)
        with patch.object(ai.http.client, 'HTTPSConnection', return_value=connection) as factory:
            with self.assertRaises(ai.APIError): ai.request_review(self.payload, replace(self.config, api_url='https://cloud.invalid/v1'))
        factory.assert_called_once_with('cloud.invalid', None, timeout=5)
        self.assertEqual(connection.request.call_args.kwargs['headers']['Authorization'], 'Bearer fixture-key')
        connection.request.assert_called_once()
        connection.close.assert_called_once()

    def test_invalid_answers_do_not_become_success(self):
        for content in ('not json', '{"answers":{}}', '{"answers":{"file_0":{"choice":"invented","confidence":1}}}'):
            with patch.object(ai.http.client, 'HTTPConnection', return_value=self.mock_response(content)):
                with self.assertRaises(ValueError): ai.request_review(self.payload, self.config)
        with patch.object(ai.http.client, 'HTTPConnection', return_value=self.mock_response('{"answers":{"file_0":{"choice":"ordinary","confidence":2}}}')):
            result = ai.request_review(self.payload, self.config)
        with self.assertRaises(ValueError): ai.validate_response(result, self.payload['questions'])

    def test_real_loopback_server_with_local_model_protocol(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, body))
                content = json.dumps({'answers': {'file_0': {'choice': 'ordinary', 'confidence': .9}}})
                response = json.dumps({'model': 'local-fixture', 'choices': [{'message': {'content': content}}]}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(response)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            config = replace(self.config, api_url=f'http://127.0.0.1:{server.server_port}/v1')
            result = ai.request_review(self.payload, config)
            ai.validate_response(result, self.payload['questions'])
            self.assertEqual(requests[0][0], '/v1/chat/completions')
            self.assertFalse(requests[0][1]['stream'])
            self.assertEqual(result['model'], 'local-fixture')
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_key_redaction_and_provider_cache_separation(self):
        with patch.dict(os.environ, {'AI_API_KEY': 'sensitive-ai-key'}):
            self.assertNotIn('sensitive-ai-key', app.redact('Authorization: Bearer sensitive-ai-key'))
        store = app.Store(':memory:')
        try:
            log = app.JsonLogger('fixture', store)
            def answer(payload, config):
                return {'model': 'local', 'answers': {key: {'type': 'choice', 'choice': next(iter(q['criteria'])), 'confidence': 1,
                    'probabilities': {option: int(i == 0) for i, option in enumerate(q['criteria'])}} for key, q in payload['questions'].items()},
                    'usage': {'input_tokens': 0, 'output_tokens': 0}}
            with patch.object(ai, 'request_review', side_effect=answer) as request, redirect_stdout(io.StringIO()):
                for url in ('http://localhost:1', 'http://localhost:2'):
                    reviewer = ai.ShadowReviewer(replace(self.config, api_url=url), log, app.redact)
                    reviewer.versions('Artist - Song', [{'filename': 'Song.mp3'}], None)
                self.assertEqual(request.call_count, 2)
        finally: store.close()

    def test_invalid_model_reply_gets_one_budgeted_retry(self):
        store=app.Store(':memory:')
        try:
            log=app.JsonLogger('fixture',store)
            reviewer=ai.ShadowReviewer(self.config,log,app.redact)
            with patch.object(ai,'request_review',side_effect=ValueError('invalid reply')) as request, redirect_stdout(io.StringIO()):
                reviewer.versions('Artist - Song',[{'filename':'Song.mp3'}],None)
            self.assertEqual(request.call_count,2)
            self.assertEqual(reviewer.calls,2)
            row=store.db.execute('SELECT status,attempts FROM ai_judgments').fetchone()
            self.assertEqual(tuple(row),('ERROR',2))
        finally: store.close()
