# Davi Lima de Medeiros  

import os
import re

from migen import *
from migen.fhdl.specials import Tristate

from litex.gen import *

from litex import get_data_mod
from litex.soc.interconnect import wishbone, stream
from litex.soc.interconnect.csr import *
from litex.soc.cores.cpu import CPU, CPU_GCC_TRIPLE_RISCV32



CPU_VARIANTS = ["standard", "standard+fpu"]

# GCC Flags ----------------------------------------------------------------------------------------

GCC_FLAGS = {
    #                       /------------ Base ISA
    #                       |    /------- Hardware Multiply + Divide
    #                       |    |/----- Atomics
    #                       |    ||/---- Compressed ISA
    #                       |    |||/--- Single-Precision Floating-Point
    #                       |    ||||/-- Double-Precision Floating-Point
    #                       i    macfd
    "standard": "-march=rv32i2p0_mc    -mabi=ilp32 ",
    "standard+fpu": "-march=rv32i2p0_mfc   -mabi=ilp32 ",
}

# OBI / APB / Trace Layouts ------------------------------------------------------------------------

obi_layout = [
    ("req",    1),
    ("gnt",    1),
    ("addr",  32),
    ("we",     1),
    ("be",     4),
    ("wdata", 32),
    ("rvalid", 1),
    ("rdata", 32),
]

apb_layout = [
    ("paddr",  32),
    ("pwdata", 32),
    ("pwrite",  1),
    ("psel",    1),
    ("penable", 1),
    ("prdata", 32),
    ("pready",  1),
    ("pslverr", 1),
]

# Helpers ------------------------------------------------------------------------------------------

def add_manifest_sources(platform, manifest):
    basedir = get_data_mod("cpu", "riscx").data_location
    with open(os.path.join(basedir, manifest), 'r') as f:
        for l in f:
            res = re.search('\$\{RTL_HOME\}/(.+)', l)
            if res and not re.match('//', l):
                if re.match('\+incdir\+', l):
                    platform.add_verilog_include_path(os.path.join(basedir, 'rtl', res.group(1)))
                else:
                    platform.add_source(os.path.join(basedir, 'rtl', res.group(1)))

# OBI <> Wishbone ----------------------------------------------------------------------------------

class OBI2Wishbone(LiteXModule):
    def __init__(self, obi, wb):
        addr  = Signal.like(obi.addr)
        be    = Signal.like(obi.be)
        we    = Signal.like(obi.we)
        wdata = Signal.like(obi.wdata)

        self.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            # On OBI request:
            If(obi.req,
                # Drive Wishbone bus from OBI bus.
                wb.adr.eq(obi.addr[2:32]),
                wb.stb.eq(            1),
                wb.dat_w.eq(  obi.wdata),
                wb.cyc.eq(            1),
                wb.sel.eq(       obi.be),
                wb.we.eq(        obi.we),

                # Store OBI bus values.
                NextValue(addr,  obi.addr),
                NextValue(be,    obi.be),
                NextValue(we,    obi.we),
                NextValue(wdata, obi.wdata),

                # Now we need to wait Wishbone Ack.
                NextState("ACK")
            ),
            obi.gnt.eq(1), # Always ack OBI request in Idle.
        )
        fsm.act("ACK",
            # Drive Wishbone bus from stored OBI bus values.
            wb.adr.eq(addr[2:32]),
            wb.stb.eq(         1),
            wb.dat_w.eq(   wdata),
            wb.cyc.eq(         1),
            wb.sel.eq(        be),
            wb.we.eq(         we),

            # On Wishbone Ack:
            If(wb.ack,
                # Generate OBI response.
                obi.rvalid.eq(1),
                obi.rdata.eq(wb.dat_r),

                # Return to Idle.
                NextState("IDLE")
            )
        )

class Wishbone2OBI(LiteXModule):
    def __init__(self, wb, obi):
        self.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(wb.cyc & wb.stb,
                obi.req.eq(1),
                NextState("ACK"),
            )
        )
        fsm.act("ACK",
            wb.ack.eq(1),
            NextState("IDLE"),
        )

        self.comb += [
            obi.we.eq(wb.we),
            obi.be.eq(wb.sel),
            obi.addr.eq(Cat(Signal(2), wb.adr)),
            obi.wdata.eq(wb.dat_w),
            wb.dat_r.eq(obi.rdata),
        ]

# Wishbone <> APB ----------------------------------------------------------------------------------

class Wishbone2APB(LiteXModule):
    def __init__(self, wb, apb):
        self.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(wb.cyc & wb.stb,
                NextState("ACK"),
            )
        )
        fsm.act("ACK",
            apb.penable.eq(1),
            wb.ack.eq(1),
            NextState("IDLE"),
        )

        self.comb += [
            apb.paddr.eq(Cat(Signal(2), wb.adr)),
            apb.pwrite.eq(wb.we),
            apb.psel.eq(1),
            apb.pwdata.eq(wb.dat_w),
            wb.dat_r.eq(apb.prdata),
        ]

# Debug Module -------------------------------------------------------------------------------------

class DebugModule(LiteXModule):
    jtag_layout = [
        ("tck",  1),
        ("tms",  1),
        ("trst", 1),
        ("tdi",  1),
        ("tdo",  1),
    ]
    def __init__(self, pads=None):
        if pads is None:
            pads = Record(self.jtag_layout)
        self.pads = pads
        self.dmbus = wishbone.Interface(data_width=32, address_width=32, addressing="word")
        self.sbbus = wishbone.Interface(data_width=32, address_width=32, addressing="word")
        dmbus = Record(obi_layout)
        sbbus = Record(obi_layout)

        self.sbbus_conv = OBI2Wishbone(sbbus, self.sbbus)
        self.dmbus_conv = Wishbone2OBI(self.dmbus, dmbus)

        self.debug_req = Signal()
        self.ndmreset  = Signal()

        tdo_i  = Signal()
        tdo_o  = Signal()
        tdo_oe = Signal()

        self.specials += Tristate(pads.tdo, tdo_o, tdo_oe, tdo_i)

        self.dm_params = dict(
            # Clk / Rst.
            i_clk       = ClockSignal("sys"),
            i_rst_n     = ~ResetSignal("sys"),
            o_ndmreset  = self.ndmreset,
            o_debug_req = self.debug_req,

            # Slave Bus.
            i_dm_req    = dmbus.req,
            i_dm_we     = dmbus.we,
            i_dm_addr   = dmbus.addr,
            i_dm_be     = dmbus.be,
            i_dm_wdata  = dmbus.wdata,
            o_dm_rdata  = dmbus.rdata,

            # Master Bus.
            o_sb_req    = sbbus.req,
            o_sb_addr   = sbbus.addr,
            o_sb_we     = sbbus.we,
            o_sb_wdata  = sbbus.wdata,
            o_sb_be     = sbbus.be,
            i_sb_gnt    = sbbus.gnt,
            i_sb_rvalid = sbbus.rvalid,
            i_sb_rdata  = sbbus.rdata,

            # JTAG.
            i_tck       = pads.tck,
            i_tms       = pads.tms,
            i_trst_n    = pads.trst,
            i_tdi       = pads.tdi,
            o_tdo       = tdo_o,
            o_tdo_oe    = tdo_oe,
        )

        self.comb += [
            dmbus.gnt.eq(dmbus.req),
            dmbus.rvalid.eq(dmbus.gnt),
        ]

        self.specials += Instance("dm_wrap", **self.dm_params)

# RISCX -----------------------------------------------------------------------------------------

class RISCX(CPU):
    family               = "riscv"
    category             = "softcore"
    name                 = "riscx"
    human_name           = "RISC-X"
    variants             = CPU_VARIANTS
    data_width           = 32
    endianness           = "little"
    gcc_triple           = CPU_GCC_TRIPLE_RISCV32
    linker_output_format = "elf32-littleriscv"
    nop                  = "nop"
    io_regions           = {0x80000000: 0x80000000} # Origin, Length.
    has_fpu              = ["standard+fpu"]

    # GCC Flags.
    @property
    def gcc_flags(self):
        flags = GCC_FLAGS[self.variant]
        flags += "-D__riscx__ "
        return flags

    def __init__(self, platform, variant="standard"):
        self.platform          = platform
        self.variant           = variant
        self.reset             = Signal()
        self.ibus              = wishbone.Interface(data_width=32, address_width=32, addressing="word")
        self.dbus              = wishbone.Interface(data_width=32, address_width=32, addressing="word")
        self.periph_buses      = [self.ibus, self.dbus]
        self.memory_buses      = []
        self.interrupt         = Signal(16)
        self.interrupt_padding = Signal(16)

        ibus = Record(obi_layout)
        dbus = Record(obi_layout)

        # OBI <> Wishbone.
        self.ibus_conv = OBI2Wishbone(ibus, self.ibus)
        self.dbus_conv = OBI2Wishbone(dbus, self.dbus)

        self.comb += [
            ibus.we.eq(0),
            ibus.be.eq(1111),
        ]

        self.cpu_params = dict(
            
            i_clk_i                 = ClockSignal("sys"),
            i_rst_n_i               = ~ResetSignal("sys"),

            i_dmem_rdata_i         = dbus.rdata,
            o_dmem_wdata_o         = dbus.wdata,
            o_dmem_addr_o          = dbus.addr,
            o_dmem_wen_o           = dbus.we,
            o_dmem_ben_o           = dbus.be,

            i_imem_rdata_i         = ibus.rdata,
            o_imem_addr_o          = ibus.addr,

            i_hartid_i              = 0,
            i_mtvec_i               = 0,
            i_boot_addr_i           = 0,


        )

        # Add Verilog sources.
        if variant in self.has_fpu:
            # Specific FPU variant parameters/files.
            self.cpu_params.update(ISA_F=1)
            add_manifest_sources(platform, 'file_list.f')
        else:
            add_manifest_sources(platform, 'file_list.f')

    def add_debug_module(self, dm):
        self.cpu_params.update(i_debug_req_i=dm.debug_req)
        self.cpu_params.update(i_rst_ni=~(ResetSignal("sys") | dm.ndmreset))

    def set_reset_address(self, reset_address):
        self.reset_address = reset_address
        self.cpu_params.update(i_boot_addr_i=Signal(32, reset=reset_address))

    def do_finalize(self):
        assert hasattr(self, "reset_address")
        self.specials += Instance("core", **self.cpu_params)
