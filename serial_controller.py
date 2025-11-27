import serial
import time
import struct
import logging
import json
from database import DatabaseManager
from vmc_commands import ResponseParser, CMD_REPORT_PRODUCT

# --- Configuration ---
SERIAL_PORT = '/dev/ttyS1' 
BAUD_RATE = 57600
TIMEOUT = 0.1 

# --- Protocol Constants ---
STX = b'\xFA\xFB'
CMD_POLL = 0x41
CMD_ACK = 0x42
CMD_MACHINE_STATUS = 0x52 
CMD_GENERIC_RETURN = 0x71

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')

class VMCController:
    def __init__(self):
        self.db = DatabaseManager()
        self.ser = None
        self.current_local_pack_no = 1
        
        # State Tracking
        self.pending_action_id = None 
        self.pending_action_type = None
        self.waiting_for_ack = False # State flag
        self.last_sent_cmd_data = None # Store to handle retries

    def connect(self):
        while True:
            try:
                self.ser = serial.Serial(SERIAL_PORT, BAUD_RATE, 8, 'N', 1, timeout=TIMEOUT)
                self.ser.reset_input_buffer()
                logging.info(f"Connected to VMC on {SERIAL_PORT}")
                return
            except Exception as e:
                logging.error(f"Connection Failed: {e}. Retrying in 5s...")
                time.sleep(5)

    def calculate_checksum(self, data):
        checksum = 0
        for byte in data:
            checksum ^= byte
        return checksum

    def build_packet(self, cmd_byte, payload=b'', use_pack_no=None):
        final_payload = b''
        length_byte = 0
        
        if cmd_byte not in [CMD_POLL, CMD_ACK]:
            pack_no = use_pack_no if use_pack_no is not None else self.current_local_pack_no
            final_payload = struct.pack('B', pack_no) + payload
            length_byte = len(final_payload)

        header = STX + struct.pack('BB', cmd_byte, length_byte)
        data_to_sum = header + final_payload
        xor = self.calculate_checksum(data_to_sum)
        return data_to_sum + struct.pack('B', xor)

    def read_packet(self):
        try:
            while True:
                b = self.ser.read(1)
                if not b: return None
                if b == b'\xFA':
                    if self.ser.read(1) == b'\xFB': break
            
            header = self.ser.read(2)
            if len(header) < 2: return None
            cmd, length = struct.unpack('BB', header)
            
            payload = self.ser.read(length) if length > 0 else b''
            if len(payload) != length: return None
            
            checksum = self.ser.read(1)
            if not checksum: return None
            
            raw = STX + header + payload
            if self.calculate_checksum(raw) == ord(checksum):
                return {'cmd': cmd, 'payload': payload}
            return None
        except Exception as e:
            logging.error(f"Read Error: {e}")
            return None

    def parse_vmc_data(self, cmd, payload):
        hex_data = payload.hex().upper()
        data_body = payload[1:] if len(payload) > 0 else b''
        parsed_info = {}
        event_type = f"CMD_{hex(cmd)}"

        # Reuse the parsing logic from previous step...
        # (Shortened for brevity, assumes full parser logic is here)
        if cmd == 0x21: # Money
            parsed_info = {"mode": data_body[0], "amount": int.from_bytes(data_body[1:5], 'big')}
            logging.info(f"💵 Money In: {parsed_info['amount']}")
        elif cmd == CMD_REPORT_PRODUCT: # 0x11
            parsed_info = ResponseParser.parse_product_report(data_body)
            if parsed_info: self.db.upsert_product(parsed_info)
        elif cmd == 0x02: # Check Selection
            status_code = data_body[0]
            parsed_info = {"status_code": status_code, "msg": "Normal" if status_code==1 else "Error"}
            if self.pending_action_id and self.pending_action_type == 0x03:
                status = 'ACCEPTED' if status_code == 0x01 else 'FAILED'
                self.db.update_command_result(self.pending_action_id, status, hex_data, parsed_info)
        elif cmd == 0x04: # Dispense Status
            status_code = data_body[0]
            parsed_info = {"code": status_code}
            is_success = status_code in [0x02, 0x24]
            is_intermediate = status_code in [0x01, 0x10, 0x11, 0x12, 0x13]
            if self.pending_action_id:
                if is_intermediate: self.db.update_command_result(self.pending_action_id, 'DISPENSING', hex_data, parsed_info)
                else: self.db.update_command_result(self.pending_action_id, 'COMPLETED' if is_success else 'FAILED', hex_data, parsed_info)
        elif cmd == CMD_GENERIC_RETURN: # 0x71
            parsed_info = ResponseParser.parse_0x71_generic(data_body)
            if self.pending_action_id and parsed_info and parsed_info.get('sub_command') == self.pending_action_type:
                self.db.update_command_result(self.pending_action_id, 'COMPLETED' if parsed_info.get('success', True) else 'FAILED', hex_data, parsed_info)
        elif cmd == CMD_MACHINE_STATUS: # 0x52
             # ... existing 0x52 logic ...
             pass
        else:
            self.db.log_event(event_type, hex_data)

    def run(self):
        self.connect()
        logging.info("Daemon Running (Non-Blocking Mode)...")
        
        while True:
            packet = self.read_packet()
            
            if not packet:
                continue

            cmd = packet['cmd']

            # =================================================================
            # CASE 1: POLL (The Start AND End of a Cycle)
            # =================================================================
            if cmd == CMD_POLL:
                
                # 1. CHECK PREVIOUS CYCLE
                # If we are seeing a POLL but waiting_for_ack is True, we missed the ACK.
                if self.waiting_for_ack and self.pending_action_id:
                    logging.warning(f"Missed ACK for CMD {self.pending_action_id}. Handling Retry...")
                    # Fetch current retry count to be safe
                    # Note: We just increment here. Next cycle handles re-sending.
                    if self.last_sent_cmd_data:
                        status = self.db.increment_retry(self.pending_action_id, self.last_sent_cmd_data['retry_count'])
                        if status == 'FAILED':
                            logging.error(f"CMD {self.pending_action_id} Failed Max Retries")
                            self.pending_action_id = None
                            self.pending_action_type = None
                            self.last_sent_cmd_data = None
                        # If status is SENDING, we keep pending_action_id. 
                        # Next block will pick it up because DB status is SENDING.

                # 2. CLEAR CONTEXT (Poll terminates transaction data stream)
                self.waiting_for_ack = False
                # Note: We DO NOT clear pending_action_id here blindly, 
                # because we might be in the middle of a multi-stage dispense (waiting for 0x04 status).
                # We only clear it if we were expecting an ACK (transport layer) and finished that step.

                # 3. FETCH NEXT ACTION
                next_cmd = self.db.get_next_command()
                
                if next_cmd:
                    cmd_id = next_cmd['id']
                    raw_bytes = bytes.fromhex(next_cmd['command_hex'])
                    
                    # Logic: New vs Retry
                    is_new = (next_cmd['status'] == 'PENDING')
                    pack_no = self.current_local_pack_no if is_new else next_cmd['assigned_pack_no']
                    
                    if is_new: 
                        self.db.mark_as_sending(cmd_id, pack_no)

                    # Send Command
                    packet = self.build_packet(raw_bytes[0], raw_bytes[1:], use_pack_no=pack_no)
                    self.ser.write(packet)
                    
                    # Update State
                    self.pending_action_id = cmd_id
                    self.pending_action_type = raw_bytes[0]
                    self.last_sent_cmd_data = next_cmd
                    self.waiting_for_ack = True # Non-blocking wait
                    
                    # NO NESTED READ HERE! We loop back.
                    
                else:
                    # Idle Heartbeat
                    self.ser.write(self.build_packet(CMD_ACK))

            # =================================================================
            # CASE 2: ACK (Receipt Confirmation)
            # =================================================================
            elif cmd == CMD_ACK:
                if self.waiting_for_ack:
                    # Successful Transport
                    self.db.update_command_result(self.pending_action_id, 'ACKED')
                    self.waiting_for_ack = False
                    self.current_local_pack_no = (self.current_local_pack_no % 255) + 1
                    logging.info(f"ACK Received for CMD {self.pending_action_id}")
                else:
                    logging.debug("Received stray ACK (Ignored)")

            # =================================================================
            # CASE 3: DATA (Responses & Events)
            # =================================================================
            else:
                # Process data immediately
                self.parse_vmc_data(cmd, packet['payload'])
                
                # Protocol says we must ACK data
                self.ser.write(self.build_packet(CMD_ACK))

if __name__ == "__main__":
    ctrl = VMCController()
    ctrl.run()