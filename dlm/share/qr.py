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
        
        # Encode room info as custom URI
        data = f"dlm://share?room={room_id}&ip={ip}&port={port}&token={token}"
        
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=1,
            border=1
        )
        qr.add_data(data)
        qr.make(fit=True)
        
        # Generate ASCII art using block characters
        output = []
        matrix = qr.get_matrix()
        
        # Add top border
        output.append("┌" + "─" * (len(matrix[0]) * 2) + "┐")
        
        # Add QR code rows
        for row in matrix:
            line = '│' + ''.join('██' if cell else '  ' for cell in row) + '│'
            output.append(line)
        
        # Add bottom border
        output.append("└" + "─" * (len(matrix[0]) * 2) + "┘")
        
        # Add room info below QR
        output.append("")
        output.append(f"  Room: {room_id}")
        output.append(f"  Token: {token}")
        output.append(f"  IP: {ip}:{port}")
        
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
""".format(room_id=room_id, token=token, ip=ip, port=port)
    
    except Exception as e:
        return f"""
QR Code Generation Error:
{str(e)}

Room Information:
  Room: {room_id}
  Token: {token}
  IP: {ip}:{port}
"""


def parse_qr_data(data: str) -> dict:
    """
    Parse QR code data back into room information.
    
    Args:
        data: QR code data string (dlm://share?...)
    
    Returns:
        Dictionary with room_id, ip, port, token
    """
    try:
        from urllib.parse import urlparse, parse_qs
        
        if not data.startswith('dlm://share'):
            raise ValueError("Invalid QR code format")
        
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
