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
TIMEOUT = 0.1 # 100ms Response Window

# --- Protocol Constants ---
STX = b'\xFA\xFB'
CMD_POLL = 0x41
CMD_ACK = 0x42
CMD_MACHINE_STATUS = 0x52 # Response to 0x51

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')

class VMCController:
    def __init__(self):
        self.db = DatabaseManager()
        self.ser = None
        self.current_local_pack_no = 1
        
        # Action Correlation Tracker
        self.pending_action_id = None 
        self.pending_action_type = None

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
            # State Machine for 0xFA 0xFB
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
            
            # Verify
            raw = STX + header + payload
            if self.calculate_checksum(raw) == ord(checksum):
                return {'cmd': cmd, 'payload': payload}
            return None
        except Exception as e:
            logging.error(f"Read Error: {e}")
            return None

    # ------------------------------------------------------------------
    #  THE PARSER: Updates DB based on whatever data flows in
    # ------------------------------------------------------------------
    def parse_vmc_data(self, cmd, payload):
        hex_data = payload.hex().upper()
        # Remove PackNO (1st byte) for data body
        data_body = payload[1:] if len(payload) > 0 else b''
        
        parsed_info = {}
        event_type = f"CMD_{hex(cmd)}"

        # --- 4.1 Payment System ---
        if cmd == 0x21: # Money Notice
            event_type = "MONEY_IN"
            mode = data_body[0]
            amount = int.from_bytes(data_body[1:5], 'big')
            parsed_info = {"mode": mode, "amount": amount}
            logging.info(f"💵 Money In: {amount}")

        # --- 4.2 Product Reporting (0x11) ---
        elif cmd == CMD_REPORT_PRODUCT:
            event_type = "PRODUCT_REPORT"
            # Use Part 3 library to parse
            product_data = ResponseParser.parse_product_report(data_body)
            if product_data:
                self.db.upsert_product(product_data)
                parsed_info = product_data
                logging.debug(f"Updated Product: {product_data['selection']}")

        # --- 4.3 Dispensing (Multi-Stage Handling) ---
        elif cmd == 0x02: # Selection Check
            event_type = "SELECTION_CHECK"
            status_code = data_body[0]
            status_map = {0x01: "Normal", 0x02: "Out of Stock", 0x03: "Invalid Selection", 0x04: "Paused"}
            msg = status_map.get(status_code, "Error")
            parsed_info = {"status_code": status_code, "message": msg}

            if self.pending_action_id and self.pending_action_type == 0x03:
                status = 'ACCEPTED' if status_code == 0x01 else 'FAILED'
                self.db.update_command_result(self.pending_action_id, status, hex_data, parsed_info)
            elif self.pending_action_id and self.pending_action_type == 0x01:
                self.db.update_command_result(self.pending_action_id, 'COMPLETED', hex_data, parsed_info)

        elif cmd == 0x04: # Dispense Status
            event_type = "DISPENSE_STATUS"
            status_code = data_body[0]
            status_map = {
                0x01: "Dispensing...", 0x02: "Success", 0x03: "Jammed", 
                0x04: "Motor Error", 0x10: "Elevator Moving", 0x24: "Success (Take)"
            }
            msg = status_map.get(status_code, f"Code {hex(status_code)}")
            parsed_info = {"status": msg, "code": status_code}

            is_success = status_code in [0x02, 0x24]
            is_intermediate = status_code in [0x01, 0x10, 0x11, 0x12, 0x13, 0x16, 0x19, 0x22, 0x23]
            
            if self.pending_action_id:
                if is_intermediate:
                    self.db.update_command_result(self.pending_action_id, 'DISPENSING', hex_data, parsed_info)
                else:
                    final_status = 'COMPLETED' if is_success else 'FAILED'
                    self.db.update_command_result(self.pending_action_id, final_status, hex_data, parsed_info)

        # --- 4.4 Machine Status (0x52) ---
        # Structure based on PDF Page 16:
        # [0] BillAcc, [1] CoinAcc, [2] CardReader, [3] TempCtrl, [4] Temp, [5] Door
        # [6-9] BillChange, [10-13] CoinChange, [14-23] MachineID
        elif cmd == CMD_MACHINE_STATUS:
            event_type = "MACHINE_STATUS_FULL"
            if len(data_body) >= 24:
                statuses = {
                    "bill_acceptor": "Error" if data_body[0] != 0 else "Normal",
                    "coin_acceptor": "Error" if data_body[1] != 0 else "Normal",
                    "card_reader": "Error" if data_body[2] != 0 else "Normal",
                    "temp_controller": "Error" if data_body[3] != 0 else "Normal",
                    "temperature": int(data_body[4]),
                    "door": "Open" if data_body[5] != 0 else "Closed" 
                }
                
                # Parse 4-byte Integers
                bill_change = int.from_bytes(data_body[6:10], 'big')
                coin_change = int.from_bytes(data_body[10:14], 'big')
                machine_id = data_body[14:24].decode('utf-8', errors='ignore').strip()

                # Update Database individually for granular tracking
                self.db.update_machine_status("temperature", statuses['temperature'], hex_data)
                self.db.update_machine_status("door", statuses['door'], hex_data)
                self.db.update_machine_status("bill_change_amount", bill_change, hex_data)
                self.db.update_machine_status("coin_change_amount", coin_change, hex_data)
                self.db.update_machine_status("machine_id", machine_id, hex_data)

                parsed_info = statuses
                parsed_info["bill_change"] = bill_change
                parsed_info["coin_change"] = coin_change
                
                # If we explicitly asked for this status (0x51), mark command complete
                if self.pending_action_id and self.pending_action_type == 0x51:
                    self.db.update_command_result(self.pending_action_id, 'COMPLETED', hex_data, parsed_info)

        # --- Fallback ---
        else:
            self.db.log_event(event_type, hex_data)

    def run(self):
        self.connect()
        logging.info("Daemon Running...")
        while True:
            packet = self.read_packet()
            if not packet: continue
            
            cmd = packet['cmd']
            if cmd == CMD_POLL:
                # POLL terminates previous transaction context
                self.pending_action_id = None
                self.pending_action_type = None

                next_cmd = self.db.get_next_command()
                if next_cmd:
                    cmd_id = next_cmd['id']
                    raw_bytes = bytes.fromhex(next_cmd['command_hex'])
                    pack_no = self.current_local_pack_no if next_cmd['status'] == 'PENDING' else next_cmd['assigned_pack_no']
                    if next_cmd['status'] == 'PENDING': self.db.mark_as_sending(cmd_id, pack_no)
                    
                    self.pending_action_id = cmd_id
                    self.pending_action_type = raw_bytes[0]
                    
                    self.ser.write(self.build_packet(raw_bytes[0], raw_bytes[1:], use_pack_no=pack_no))
                    
                    ack = self.read_packet()
                    if ack and ack['cmd'] == CMD_ACK:
                        self.db.update_command_result(cmd_id, 'ACKED')
                        self.current_local_pack_no = (self.current_local_pack_no % 255) + 1
                    else:
                        if self.db.increment_retry(cmd_id, next_cmd['retry_count']) == 'FAILED':
                            self.pending_action_id = None
                else:
                    self.ser.write(self.build_packet(CMD_ACK))
            
            elif cmd != CMD_ACK:
                self.parse_vmc_data(cmd, packet['payload'])
                self.ser.write(self.build_packet(CMD_ACK))

if __name__ == "__main__":
    ctrl = VMCController()
    ctrl.run()