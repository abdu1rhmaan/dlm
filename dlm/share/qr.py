"""QR code generation for share rooms."""

def generate_room_qr(room_id: str, ip: str, port: int, token: str) -> str:
    """
    Generate ASCII QR code for room joining.
    
    Args:
        room_id: 4-character room ID
        ip: Host IP address
        port: Server port
        token: XXX-XXX format token
    
    Returns:
        ASCII art QR code or error message
    """
    try:
        import qrcode
        
        # Encode the HTTP Invite URL as the primary payload
        data = f"http://{ip}:{port}/invite?t={token}"
        
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=1,
            border=1
        )
        qr.add_data(data)
        qr.make(fit=True)
        
        # Generate ASCII art using half-block characters (compact version)
        output = []
        matrix = qr.get_matrix()
        
        # Add top border
        width = len(matrix[0])
        output.append("┌" + "─" * width + "┐")
        
        # Iterate two rows at a time
        for r in range(0, len(matrix), 2):
            line = "│"
            for c in range(width):
                top = matrix[r][c]
                bottom = matrix[r+1][c] if r+1 < len(matrix) else False
                
                if top and bottom: line += "█"
                elif top: line += "▀"
                elif bottom: line += "▄"
                else: line += " "
            line += "│"
            output.append(line)
        
        # Add bottom border
        output.append("└" + "─" * width + "┘")
        
        # No footer link - already shown in TUI header
        return '\n'.join(output)
    
    except ImportError:
        return """
╔════════════════════════════════════╗
║  QR Code Generation Unavailable   ║
╠════════════════════════════════════╣
║                                    ║
║  Install qrcode to enable:         ║
║  pip install qrcode[pil]           ║
║                                    ║
╚════════════════════════════════════╝

Room Information:
  Room: {room_id}
  Token: {token}
  IP: {ip}:{port}
  Invite: http://{ip}:{port}/invite?t={token}
""".format(room_id=room_id, token=token, ip=ip, port=port)
    
    except Exception as e:
        return f"""
QR Code Generation Error:
{str(e)}

Room Information:
  Room: {room_id}
  Token: {token}
  IP: {ip}:{port}
  Invite: http://{ip}:{port}/invite?t={token}
"""


def parse_qr_data(data: str) -> dict:
    """
    Parse QR code data back into room information.
    
    Args:
        data: QR code data string (dlm://share?...) or Invite URL
    
    Returns:
        Dictionary with room_id, ip, port, token
    """
    try:
        from urllib.parse import urlparse, parse_qs
        
        if data.startswith('http'):
            # Allow parsing the invite URL if pasted directly
            parsed = urlparse(data)
            params = parse_qs(parsed.query)
            return {
                'ip': parsed.hostname,
                'port': parsed.port,
                'room_id': '?',
                'token': params.get('t', ['?'])[0]
            }

        if not data.startswith('dlm://'):
            raise ValueError("Invalid URI format")
        
        parsed = urlparse(data)
        params = parse_qs(parsed.query)
        
        return {
            'room_id': params.get('room', [''])[0],
            'ip': params.get('ip', [''])[0],
            'port': int(params.get('port', ['0'])[0]),
            'token': params.get('token', [''])[0]
        }
    except Exception as e:
        raise ValueError(f"Failed to parse QR data: {e}")
