import re
import shlex
from typing import List, Dict, Any, Optional

class DSLParser:
    """
    Parses the DLM Import DSL.
    
    Structure:
    - URL (Single or Playlist)
    - [Scope] (e.g. [video | 1080p])
    - Block { ... } 
    - Assignment (key=value)
    - Flag (e.g. vocals)
    """

    def __init__(self):
        self.tokens = []
        self.pos = 0

    def parse(self, text: str) -> List[Dict[str, Any]]:
        self.tokens = self._tokenize(text)
        self.pos = 0
        return self._parse_body()

    def _tokenize(self, text: str) -> List[Dict[str, Any]]:
        # Regexes for components
        token_specification = [
            ('COMMENT',    r'#.*'),
            ('URL',        r'https?://[^\s{}]+'),
            ('SCOPE',      r'\[[^\]]+\]'),
            ('LBRACE',     r'\{'),
            ('RBRACE',     r'\}'),
            ('ASSIGN',     r'[\w_-]+=[^\s{}]+'),
            ('INDEX',      r'\b\d+\b'), # Can be item index 
            ('FLAG',       r'[\w_-]+'),
            ('WHITESPACE', r'\s+'),
        ]
        
        regex = '|'.join('(?P<%s>%s)' % pair for pair in token_specification)
        tokens = []
        for mo in re.finditer(regex, text):
            kind = mo.lastgroup
            value = mo.group()
            if kind == 'WHITESPACE' or kind == 'COMMENT':
                continue
            tokens.append({'type': kind, 'value': value})
        return tokens

    def _peek(self):
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def _advance(self):
        token = self._peek()
        self.pos += 1
        return token

    def _parse_body(self) -> List[Dict[str, Any]]:
        results = []
        while self.pos < len(self.tokens):
            if self._peek()['type'] == 'RBRACE':
                break
            
            node = self._parse_statement()
            if node:
                results.append(node)
        return results

    def _parse_statement(self) -> Dict[str, Any]:
        token = self._peek()
        if not token: return None

        if token['type'] == 'URL':
            url = self._advance()['value']
            # Check for block
            block = None
            if self._peek() and self._peek()['type'] == 'LBRACE':
                self._advance() # Consume {
                block = self._parse_body()
                if self._peek() and self._peek()['type'] == 'RBRACE':
                    self._advance() # Consume }
            
            return {'type': 'url_block', 'url': url, 'body': block}

        if token['type'] == 'SCOPE':
            scope_val = self._advance()['value'].strip('[]')
            return {'type': 'scope', 'value': scope_val}

        if token['type'] == 'INDEX':
            idx = self._advance()['value']
            # Usually index is followed by a block in playlists
            block = None
            if self._peek() and self._peek()['type'] == 'LBRACE':
                self._advance()
                block = self._parse_body()
                if self._peek() and self._peek()['type'] == 'RBRACE':
                    self._advance()
            return {'type': 'item_block', 'index': int(idx), 'body': block}

        if token['type'] == 'ASSIGN':
            val = self._advance()['value']
            key, value = val.split('=', 1)
            return {'type': 'assignment', 'key': key, 'value': value}

        if token['type'] == 'FLAG':
            flag = self._advance()['value']
            return {'type': 'flag', 'value': flag}

        # Unexpected token
        self._advance()
        return None

class DSLEvaluator:
    """
    Translates parsed AST into actionable tasks with strict validation.
    """
    def __init__(self):
        self.errors = []

    def evaluate(self, ast: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        self.errors = []
        tasks = []
        global_context = {
            'mode': None,
            'quality': None,
            'cut': None,
            'vocals': False,
            'gpu': False,
            'output': None
        }
        
        # Pass 1: Global Scopes and Assignments
        for node in ast:
            if node['type'] == 'scope':
                self._update_context_from_scope(global_context, node['value'])
            elif node['type'] == 'flag':
                if node['value'] == 'vocals': global_context['vocals'] = True
                if node['value'] == 'gpu': global_context['gpu'] = True
            elif node['type'] == 'assignment':
                global_context[node['key']] = node['value']
        
        # Pass 2: URL Blocks
        for node in ast:
            if node['type'] == 'url_block':
                task = self._process_url_node(node, global_context.copy())
                if task:
                    tasks.append(task)
        
        return tasks

    def _process_url_node(self, node: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        url = node['url']
        config = context.copy()
        item_overrides = {}
        
        has_items = any(child['type'] == 'item_block' for child in (node['body'] or []))
        
        if node['body']:
            for child in node['body']:
                if child['type'] == 'scope':
                    self._update_context_from_scope(config, child['value'])
                elif child['type'] == 'flag':
                    if child['value'] == 'vocals': config['vocals'] = True
                elif child['type'] == 'assignment':
                    # VALIDATION: No cut at playlist root if items exist (or if it's a playlist URL)
                    if child['key'] in ['cut', 'segment'] and (has_items or 'list=' in url.lower()):
                        self.errors.append(f"Error in {url}: '{child['key']}' is not allowed at the playlist level.")
                        continue
                    config[child['key']] = child['value']
                elif child['type'] == 'item_block':
                    item_config = config.copy()
                    if child['body']:
                        for item_child in child['body']:
                             if item_child['type'] == 'scope':
                                 self._update_context_from_scope(item_config, item_child['value'])
                             elif item_child['type'] == 'flag':
                                 if item_child['value'] == 'vocals': item_config['vocals'] = True
                             elif item_child['type'] == 'assignment':
                                 item_config[item_child['key']] = item_child['value']
                    
                    # VALIDATION: Audio + Cut
                    if item_config.get('mode') == 'audio' and item_config.get('cut'):
                        self.errors.append(f"Error in {url} item {child['index']}: 'cut' is not allowed with [audio].")
                        
                    item_overrides[child['index']] = item_config

        # Final check for simple URL block (no items)
        if not item_overrides and config.get('mode') == 'audio' and config.get('cut'):
             self.errors.append(f"Error in {url}: 'cut' is not allowed with [audio].")

        return {
            'url': url,
            'config': config,
            'overrides': item_overrides
        }

    def _update_context_from_scope(self, ctx: Dict[str, Any], scope_str: str):
        parts = [p.strip().lower() for p in scope_str.split('|')]
        for p in parts:
            if p == 'video': ctx['mode'] = 'video'
            elif p == 'audio': ctx['mode'] = 'audio'
            elif p.endswith('p') and p[:-1].isdigit():
                ctx['quality'] = p
            elif p == 'best':
                ctx['quality'] = 'best'
