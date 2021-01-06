from brownie import *
from tabulate import tabulate
from rich.console import Console

from helpers.constants import *
from helpers.multicall import Call, Multicall, as_wei, func
from helpers.registry import registry
from helpers.sett.resolvers import (
    SettCoreResolver,
    StrategyBadgerLpMetaFarmResolver,
    StrategyHarvestMetaFarmResolver,
    StrategySushiBadgerWbtcResolver,
    StrategyBadgerRewardsResolver,
    StrategySushiBadgerLpOptimizerResolver,
    StrategyCurveGaugeResolver,
    StrategyDiggRewardsResolver,
)
from helpers.utils import val
from scripts.systems.badger_system import BadgerSystem
from scripts.systems.constants import SettType

console = Console()


def get_expected_strategy_deposit_location(badger: BadgerSystem, id):
    if id == "native.badger":
        # Rewards Staking
        return badger.getSettRewards("native.badger")
    if id == "native.uniBadgerWbtc":
        # Rewards Staking
        return badger.getSettRewards("native.uniBadgerWbtc")
    if id == "native.renCrv":
        # CRV Gauge
        return registry.curve.pools.renCrv.gauge
    if id == "native.sbtcCrv":
        # CRV Gauge
        return registry.curve.pools.sbtcCrv.gauge
    if id == "native.tbtcCrv":
        # CRV Gauge
        return registry.curve.pools.tbtcCrv.gauge
    if id == "harvest.renCrv":
        # Harvest Vault
        return registry.harvest.vaults.renCrv


def is_curve_gauge_variant(name):
    return (
        name == "StrategyCurveGaugeRenBtcCrv"
        or name == "StrategyCurveGaugeSbtcCrv"
        or name == "StrategyCurveGaugeTbtcCrv"
        or name == "StrategyCurveGaugex"
    )


class Snap:
    def __init__(self, data, block):
        self.data = data
        self.block = block

    # ===== Getters =====

    def balances(self, tokenKey, accountKey):
        return self.data["balances." + tokenKey + "." + accountKey]

    def sumBalances(self, tokenKey, accountKeys):
        total = 0
        for accountKey in accountKeys:
            total += self.data["balances." + tokenKey + "." + accountKey]
        return total

    def get(self, key):

        if not key in self.data.keys():
            assert False
        return self.data[key]

    # ===== Setters =====

    def set(self, key, value):
        self.data[key] = value


class SnapshotManager:
    def __init__(self, badger: BadgerSystem, key):
        self.badger = badger
        self.key = key
        self.sett = badger.getSett(key)
        self.strategy = badger.getStrategy(key)
        self.controller = Controller.at(self.sett.controller())
        self.want = interface.IERC20(self.sett.token())
        self.resolver = self.init_resolver(self.strategy.getName())
        self.snaps = {}
        self.settSnaps = {}
        self.entities = {}

        assert self.want == self.strategy.want()

        # Common entities for all strategies
        self.addEntity("sett", self.sett.address)
        self.addEntity("strategy", self.strategy.address)
        self.addEntity("controller", self.controller.address)
        self.addEntity("governance", self.strategy.governance())
        self.addEntity("governanceRewards", self.controller.rewards())
        self.addEntity("strategist", self.strategy.strategist())

        destinations = self.resolver.get_strategy_destinations()
        for key, dest in destinations.items():
            self.addEntity(key, dest)

    def snap(self, trackedUsers=None):
        print("snap")
        snapBlock = chain.height
        entities = self.entities

        if trackedUsers:
            for key, user in trackedUsers.items():
                entities[key] = user

        calls = []
        calls = self.resolver.add_balances_snap(calls, entities)
        calls = self.resolver.add_sett_snap(calls)
        # calls = self.resolver.add_sett_permissions_snap(calls)
        calls = self.resolver.add_strategy_snap(calls)

        multi = Multicall(calls)

        # for call in calls:
        #     print(call.target, call.function, call.args)

        data = multi()
        self.snaps[snapBlock] = Snap(data, snapBlock)

        return self.snaps[snapBlock]

    def addEntity(self, key, entity):
        self.entities[key] = entity

    def init_sett_resolver(self, version):
        print("init_sett_resolver", version)
        return SettCoreResolver(self)

    def init_resolver(self, name):
        print("init_resolver", name)
        if name == "StrategyHarvestMetaFarm":
            return StrategyHarvestMetaFarmResolver(self)
        if name == "StrategyBadgerRewards":
            return StrategyBadgerRewardsResolver(self)
        if name == "StrategyBadgerLpMetaFarm":
            return StrategyBadgerLpMetaFarmResolver(self)
        if is_curve_gauge_variant(name):
            return StrategyCurveGaugeResolver(self)
        if name == "StrategyCurveGauge":
            return StrategyCurveGaugeResolver(self)
        if name == "StrategySushiBadgerWbtc":
            return StrategySushiBadgerWbtcResolver(self)
        if name == "StrategySushiLpOptimizer":
            print("StrategySushiBadgerLpOptimizerResolver")
            return StrategySushiBadgerLpOptimizerResolver(self)
        if name == "StrategyDiggRewards":
            return StrategyDiggRewardsResolver(self)

    def settTend(self, overrides, confirm=True):
        user = overrides["from"].address
        trackedUsers = {"user": user}
        before = self.snap(trackedUsers)
        self.strategy.tend(overrides)
        after = self.snap(trackedUsers)
        if confirm:
            self.resolver.confirm_tend(before, after)

    def settHarvest(self, overrides, confirm=True):
        user = overrides["from"].address
        trackedUsers = {"user": user}
        before = self.snap(trackedUsers)
        tx = self.strategy.harvest(overrides)
        after = self.snap(trackedUsers)
        if confirm:
            self.resolver.confirm_harvest(before, after, tx)

    def settDeposit(self, amount, overrides, confirm=True):
        user = overrides["from"].address
        trackedUsers = {"user": user}
        before = self.snap(trackedUsers)
        self.sett.deposit(amount, overrides)
        after = self.snap(trackedUsers)

        params = {
            "user": user,
            "amount": amount,
            "sett_type": self.badger.sett_type,
        }

        if self.badger.sett_type == SettType.DIGG:
            # Calculate the # of digg shares (static w.r.t. rebases).
            params["shares"] = self.badger.digg_system.token.fragmentsToShares(amount)

        if confirm:
            self.resolver.confirm_deposit(
                before, after, params)

    def settDepositAll(self, overrides, confirm=True):
        user = overrides["from"].address
        trackedUsers = {"user": user}
        userBalance = self.want.balanceOf(user)
        before = self.snap(trackedUsers)
        self.sett.depositAll(overrides)
        after = self.snap(trackedUsers)

        params = {
            "user": user,
            "amount": userBalance,
            "sett_type": self.badger.sett_type,
        }
        if self.badger.sett_type == SettType.DIGG:
            # Calculate the # of digg shares (static w.r.t. rebases).
            params["shares"] = self.badger.digg_system.token.fragmentsToShares(amount)

        if confirm:
            self.resolver.confirm_deposit(
                before, after, params)

    def settEarn(self, overrides, confirm=True):
        user = overrides["from"].address
        trackedUsers = {"user": user}
        before = self.snap(trackedUsers)
        tx = self.sett.earn(overrides)
        after = self.snap(trackedUsers)
        if confirm:
            self.resolver.confirm_earn(before, after, {
                "user": user,
                "sett_type": self.badger.sett_type,
            })

    def settWithdraw(self, amount, overrides, confirm=True):
        user = overrides["from"].address
        trackedUsers = {"user": user}
        before = self.snap(trackedUsers)
        self.sett.withdraw(amount, overrides)
        after = self.snap(trackedUsers)

        params = {
            "user": user,
            "amount": amount,
            "sett_type": self.badger.sett_type,
        }

        if self.badger.sett_type == SettType.DIGG:
            # Calculate the # of digg shares (static w.r.t. rebases).
            params["shares"] = self.badger.digg_system.token.fragmentsToShares(amount)

        if confirm:
            self.resolver.confirm_withdraw(
                before, after, params)

    def settWithdrawAll(self, overrides, confirm=True):
        user = overrides["from"].address
        trackedUsers = {"user": user}
        userBalance = self.sett.balanceOf(user)
        before = self.snap(trackedUsers)
        self.sett.withdraw(userBalance, overrides)
        after = self.snap(trackedUsers)

        params = {
            "user": user,
            "amount": userBalance,
            "sett_type": self.badger.sett_type,
        }

        if self.badger.sett_type == SettType.DIGG:
            # Calculate the # of digg shares (static w.r.t. rebases).
            params["shares"] = self.badger.digg_system.token.fragmentsToShares(amount)

        if confirm:
            self.resolver.confirm_withdraw(
                before, after, params)

    def format(self, key, value):
        if type(value) is int:
            if (
                "balance" in key
                or key == "sett.available"
                or key == "sett.pricePerFullShare"
                or key == "sett.totalSupply"
            ):
                return val(value)
        return value

    def diff(self, a, b):
        if type(a) is int and type(b) is int:
            return b - a
        else:
            return "-"

    def printCompare(self, before: Snap, after: Snap):
        # self.printPermissions()
        table = []
        console.print(
            "[green]=== Compare: {} Sett {} -> {} ===[/green]".format(
                self.key, before.block, after.block
            )
        )

        for key, item in before.data.items():

            a = item
            b = after.get(key)

            # Don't add items that don't change
            if a != b:
                table.append(
                    [
                        key,
                        self.format(key, a),
                        self.format(key, b),
                        self.format(key, self.diff(a, b)),
                    ]
                )

        print(
            tabulate(
                table, headers=["metric", "before", "after", "diff"], tablefmt="grid"
            )
        )

    def printPermissions(self):
        # Accounts
        table = []
        console.print("[blue]=== Permissions: {} Sett ===[/blue]".format(self.key))

        table.append(["sett.keeper", self.sett.keeper()])
        table.append(["sett.governance", self.sett.governance()])
        table.append(["sett.strategist", self.sett.strategist()])

        table.append(["---------------", "--------------------"])

        table.append(["strategy.keeper", self.strategy.keeper()])
        table.append(["strategy.governance", self.strategy.governance()])
        table.append(["strategy.strategist", self.strategy.strategist()])
        table.append(["strategy.guardian", self.strategy.guardian()])

        table.append(["---------------", "--------------------"])
        print(tabulate(table, headers=["account", "value"]))

    def printBasics(self, snap: Snap):
        table = []
        console.print("[green]=== Status Report: {} Sett ===[green]".format(self.key))

        table.append(["sett.pricePerFullShare", snap.get("sett.pricePerFullShare")])
        table.append(["strategy.want", snap.balances("want", "strategy")])

        print(tabulate(table, headers=["metric", "value"]))

    def printTable(self, snap: Snap):
        # Numerical Data
        table = []
        console.print("[green]=== Status Report: {} Sett ===[green]".format(self.key))

        for key, item in snap.data.items():
            # Don't display 0 balances:
            if "balances" in key and item == 0:
                continue
            table.append([key, item])

        table.append(["---------------", "--------------------"])
        print(tabulate(table, headers=["metric", "value"]))
