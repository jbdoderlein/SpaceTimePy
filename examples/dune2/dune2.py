#!/usr/bin/env python3
"""
Dune II: The Building of a Dynasty - A Real-Time Strategy Game Clone
A feature-rich demonstration with buildings, vehicles, infantry, and resource harvesting.
Built with Python and Pygame, integrated with SpaceTimePy monitoring.
"""
import base64
import io
import random
import math
import pygame
import spacetimepy
from enum import Enum, auto
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field

# Initialize pygame
pygame.init()
random.seed(42)

# =============================================================================
# CONSTANTS
# =============================================================================

# Screen dimensions
SCREEN_WIDTH = 1920
SCREEN_HEIGHT = 1080
SIDEBAR_WIDTH = 200
MAP_WIDTH = SCREEN_WIDTH - SIDEBAR_WIDTH
MAP_HEIGHT = SCREEN_HEIGHT

# Tile settings
TILE_SIZE = 32
MAP_TILES_X = MAP_WIDTH // TILE_SIZE
MAP_TILES_Y = MAP_HEIGHT // TILE_SIZE

# Colors
COLORS = {
    'sand': (194, 178, 128),
    'sand_dark': (180, 165, 115),
    'rock': (128, 128, 128),
    'rock_dark': (100, 100, 100),
    'spice': (255, 140, 0),
    'spice_rich': (255, 100, 0),
    'building_plate': (80, 80, 80),
    'atreides': (0, 100, 200),
    'atreides_light': (50, 150, 255),
    'harkonnen': (200, 50, 50),
    'harkonnen_light': (255, 100, 100),
    'ordos': (0, 150, 0),
    'ordos_light': (50, 200, 50),
    'sidebar': (60, 60, 60),
    'sidebar_border': (100, 100, 100),
    'text': (255, 255, 255),
    'text_gold': (255, 215, 0),
    'minimap_bg': (30, 30, 30),
    'health_green': (0, 255, 0),
    'health_yellow': (255, 255, 0),
    'health_red': (255, 0, 0),
    'selection': (0, 255, 0),
    'fog': (50, 50, 50),
}

# Game settings
FPS = 60
STARTING_CREDITS = 5000
SPICE_VALUE = 25
HARVESTER_CAPACITY = 700

# =============================================================================
# ENUMS
# =============================================================================

class Faction(Enum):
    ATREIDES = auto()
    HARKONNEN = auto()
    ORDOS = auto()

class TerrainType(Enum):
    SAND = auto()
    ROCK = auto()
    SPICE = auto()
    SPICE_RICH = auto()
    BUILDING_PLATE = auto()

class UnitType(Enum):
    # Infantry
    LIGHT_INFANTRY = auto()
    HEAVY_TROOPER = auto()
    ENGINEER = auto()
    # Light Vehicles
    TRIKE = auto()
    QUAD = auto()
    # Tanks
    COMBAT_TANK = auto()
    SIEGE_TANK = auto()
    MISSILE_TANK = auto()
    # Special
    HARVESTER = auto()
    MCV = auto()
    CARRYALL = auto()
    ORNITHOPTER = auto()
    SONIC_TANK = auto()
    DEVASTATOR = auto()

class BuildingType(Enum):
    CONSTRUCTION_YARD = auto()
    WIND_TRAP = auto()
    REFINERY = auto()
    SILO = auto()
    BARRACKS = auto()
    LIGHT_FACTORY = auto()
    HEAVY_FACTORY = auto()
    HIGH_TECH_FACTORY = auto()
    REPAIR_PAD = auto()
    RADAR_OUTPOST = auto()
    TURRET = auto()
    ROCKET_TURRET = auto()
    PALACE = auto()
    WALL = auto()
    STARPORT = auto()

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class UnitStats:
    name: str
    health: int
    speed: float
    damage: int
    range: int
    cost: int
    build_time: int
    is_infantry: bool = False
    is_air: bool = False
    can_harvest: bool = False

@dataclass
class BuildingStats:
    name: str
    health: int
    cost: int
    build_time: int
    power: int  # Positive = produces, negative = consumes
    size: Tuple[int, int]  # (width, height) in tiles
    provides: List[str] = field(default_factory=list)

# Unit configurations
UNIT_STATS: Dict[UnitType, UnitStats] = {
    UnitType.LIGHT_INFANTRY: UnitStats("Light Infantry", 50, 1.0, 8, 3, 60, 5, is_infantry=True),
    UnitType.HEAVY_TROOPER: UnitStats("Heavy Trooper", 80, 0.8, 20, 4, 100, 8, is_infantry=True),
    UnitType.ENGINEER: UnitStats("Engineer", 30, 1.2, 0, 0, 400, 10, is_infantry=True),
    UnitType.TRIKE: UnitStats("Trike", 100, 3.5, 15, 4, 250, 8),
    UnitType.QUAD: UnitStats("Quad", 150, 2.8, 25, 5, 350, 10),
    UnitType.COMBAT_TANK: UnitStats("Combat Tank", 300, 2.0, 40, 5, 500, 15),
    UnitType.SIEGE_TANK: UnitStats("Siege Tank", 400, 1.5, 60, 6, 700, 20),
    UnitType.MISSILE_TANK: UnitStats("Missile Tank", 200, 1.8, 80, 8, 600, 18),
    UnitType.HARVESTER: UnitStats("Harvester", 350, 1.2, 0, 0, 400, 12, can_harvest=True),
    UnitType.MCV: UnitStats("MCV", 500, 0.8, 0, 0, 1000, 30),
    UnitType.CARRYALL: UnitStats("Carryall", 150, 4.0, 0, 0, 600, 15, is_air=True),
    UnitType.ORNITHOPTER: UnitStats("Ornithopter", 100, 5.0, 30, 3, 450, 12, is_air=True),
    UnitType.SONIC_TANK: UnitStats("Sonic Tank", 250, 1.5, 70, 5, 800, 25),
    UnitType.DEVASTATOR: UnitStats("Devastator", 600, 0.6, 100, 6, 1200, 35),
}

# Building configurations
BUILDING_STATS: Dict[BuildingType, BuildingStats] = {
    BuildingType.CONSTRUCTION_YARD: BuildingStats("Construction Yard", 1000, 0, 0, 0, (3, 3), ["buildings"]),
    BuildingType.WIND_TRAP: BuildingStats("Wind Trap", 300, 300, 10, 100, (2, 2), ["power"]),
    BuildingType.REFINERY: BuildingStats("Refinery", 500, 400, 15, -30, (3, 2), ["harvester", "spice"]),
    BuildingType.SILO: BuildingStats("Silo", 200, 150, 5, -5, (2, 2), ["storage"]),
    BuildingType.BARRACKS: BuildingStats("Barracks", 400, 400, 12, -20, (2, 2), ["infantry"]),
    BuildingType.LIGHT_FACTORY: BuildingStats("Light Factory", 500, 500, 18, -30, (2, 3), ["light_vehicles"]),
    BuildingType.HEAVY_FACTORY: BuildingStats("Heavy Factory", 600, 700, 25, -50, (3, 3), ["heavy_vehicles"]),
    BuildingType.HIGH_TECH_FACTORY: BuildingStats("High-Tech Factory", 500, 600, 20, -40, (3, 2), ["aircraft"]),
    BuildingType.REPAIR_PAD: BuildingStats("Repair Pad", 400, 500, 15, -25, (3, 2), ["repair"]),
    BuildingType.RADAR_OUTPOST: BuildingStats("Radar Outpost", 400, 450, 12, -35, (2, 2), ["radar"]),
    BuildingType.TURRET: BuildingStats("Gun Turret", 300, 250, 8, -10, (1, 1), ["defense"]),
    BuildingType.ROCKET_TURRET: BuildingStats("Rocket Turret", 350, 400, 12, -15, (1, 1), ["defense"]),
    BuildingType.PALACE: BuildingStats("Palace", 800, 1000, 40, -80, (3, 3), ["special"]),
    BuildingType.WALL: BuildingStats("Wall", 100, 50, 2, 0, (1, 1), ["defense"]),
    BuildingType.STARPORT: BuildingStats("Starport", 600, 800, 30, -60, (3, 3), ["starport"]),
}

# =============================================================================
# GAME CLASSES
# =============================================================================

class Projectile:
    """Represents a projectile in flight"""
    def __init__(self, x: float, y: float, target_x: float, target_y: float, 
                 damage: int, speed: float = 8.0, color: Tuple[int, int, int] = (255, 255, 0)):
        self.x = x
        self.y = y
        self.target_x = target_x
        self.target_y = target_y
        self.damage = damage
        self.speed = speed
        self.color = color
        self.active = True
        
        # Calculate direction
        dx = target_x - x
        dy = target_y - y
        dist = math.sqrt(dx*dx + dy*dy)
        if dist > 0:
            self.vx = (dx / dist) * speed
            self.vy = (dy / dist) * speed
        else:
            self.vx = 0
            self.vy = 0
    
    def update(self) -> bool:
        """Update projectile position, return True if reached target"""
        self.x += self.vx
        self.y += self.vy
        
        # Check if reached target
        dist = math.sqrt((self.target_x - self.x)**2 + (self.target_y - self.y)**2)
        if dist < self.speed:
            self.active = False
            return True
        return False
    
    def draw(self, surface: pygame.Surface):
        """Draw the projectile"""
        pygame.draw.circle(surface, self.color, (int(self.x), int(self.y)), 3)


class Unit:
    """Represents a game unit (infantry, vehicle, aircraft)"""
    def __init__(self, unit_type: UnitType, faction: Faction, x: float, y: float):
        self.unit_type = unit_type
        self.faction = faction
        self.stats = UNIT_STATS[unit_type]
        self.x = x
        self.y = y
        self.health = self.stats.health
        self.max_health = self.stats.health
        self.selected = False
        self.target_x: Optional[float] = None
        self.target_y: Optional[float] = None
        self.attack_target: Optional['Unit'] = None
        self.attack_cooldown = 0
        self.spice_carried = 0
        self.harvesting = False
        self.returning_to_refinery = False
        self.angle = 0
        self.state = "idle"
        
    @property
    def rect(self) -> pygame.Rect:
        size = 20 if self.stats.is_infantry else 32
        return pygame.Rect(self.x - size//2, self.y - size//2, size, size)
    
    def get_color(self) -> Tuple[int, int, int]:
        if self.faction == Faction.ATREIDES:
            return COLORS['atreides']
        elif self.faction == Faction.HARKONNEN:
            return COLORS['harkonnen']
        else:
            return COLORS['ordos']
    
    def move_towards(self, target_x: float, target_y: float, dt: float):
        """Move unit towards target position"""
        dx = target_x - self.x
        dy = target_y - self.y
        dist = math.sqrt(dx*dx + dy*dy)
        
        if dist > 2:
            speed = self.stats.speed * 30 * dt
            self.x += (dx / dist) * min(speed, dist)
            self.y += (dy / dist) * min(speed, dist)
            self.angle = math.atan2(dy, dx)
            return False
        return True
    
    def update(self, dt: float, game: 'Game') -> Optional[Projectile]:
        """Update unit state, return projectile if attacking"""
        projectile = None
        
        # Decrease attack cooldown
        if self.attack_cooldown > 0:
            self.attack_cooldown -= dt
        
        # Harvester logic
        if self.stats.can_harvest and self.faction == Faction.ATREIDES:
            self._update_harvester(dt, game)
        
        # Movement
        if self.target_x is not None and self.target_y is not None:
            if self.move_towards(self.target_x, self.target_y, dt):
                self.target_x = None
                self.target_y = None
                self.state = "idle"
            else:
                self.state = "moving"
        
        # Attack logic
        if self.attack_target and self.stats.damage > 0:
            if self.attack_target.health <= 0:
                self.attack_target = None
            else:
                dist = math.sqrt((self.attack_target.x - self.x)**2 + 
                               (self.attack_target.y - self.y)**2)
                attack_range = self.stats.range * TILE_SIZE
                
                if dist <= attack_range:
                    if self.attack_cooldown <= 0:
                        # Fire projectile
                        projectile = Projectile(
                            self.x, self.y,
                            self.attack_target.x, self.attack_target.y,
                            self.stats.damage
                        )
                        self.attack_cooldown = 1.0
                        self.state = "attacking"
                else:
                    # Move closer
                    self.target_x = self.attack_target.x
                    self.target_y = self.attack_target.y
        
        return projectile
    
    def _update_harvester(self, dt: float, game: 'Game'):
        """Harvester-specific logic"""
        if self.returning_to_refinery:
            # Find nearest refinery
            refinery = game.get_nearest_building(self.x, self.y, BuildingType.REFINERY, self.faction)
            if refinery:
                if self.target_x is None:
                    self.target_x = refinery.x + refinery.stats.size[0] * TILE_SIZE / 2
                    self.target_y = refinery.y + refinery.stats.size[1] * TILE_SIZE / 2
                
                dist = math.sqrt((self.target_x - self.x)**2 + (self.target_y - self.y)**2)
                if dist < TILE_SIZE:
                    # Unload spice
                    game.players[self.faction].credits += self.spice_carried * SPICE_VALUE // HARVESTER_CAPACITY
                    self.spice_carried = 0
                    self.returning_to_refinery = False
                    self.harvesting = True
                    self.target_x = None
                    self.target_y = None
        
        elif self.harvesting or self.state == "idle":
            # Find spice
            tile_x = int(self.x // TILE_SIZE)
            tile_y = int(self.y // TILE_SIZE)
            
            if 0 <= tile_x < MAP_TILES_X and 0 <= tile_y < MAP_TILES_Y:
                terrain = game.terrain[tile_y][tile_x]
                if terrain in (TerrainType.SPICE, TerrainType.SPICE_RICH):
                    # Harvest spice
                    self.harvesting = True
                    self.state = "harvesting"
                    harvest_rate = 50 if terrain == TerrainType.SPICE_RICH else 25
                    self.spice_carried += int(harvest_rate * dt)
                    
                    # Deplete spice occasionally
                    if random.random() < 0.001:
                        game.terrain[tile_y][tile_x] = TerrainType.SAND
                    
                    if self.spice_carried >= HARVESTER_CAPACITY:
                        self.returning_to_refinery = True
                        self.harvesting = False
                else:
                    # Look for spice
                    spice_pos = game.find_nearest_spice(self.x, self.y)
                    if spice_pos and self.target_x is None:
                        self.target_x = spice_pos[0] * TILE_SIZE + TILE_SIZE // 2
                        self.target_y = spice_pos[1] * TILE_SIZE + TILE_SIZE // 2
                        self.harvesting = True
    
    def take_damage(self, damage: int):
        """Apply damage to unit"""
        self.health -= damage
        if self.health < 0:
            self.health = 0
    
    def draw(self, surface: pygame.Surface):
        """Draw the unit"""
        color = self.get_color()
        size = 20 if self.stats.is_infantry else 32
        
        if self.stats.is_infantry:
            # Draw infantry as circles
            pygame.draw.circle(surface, color, (int(self.x), int(self.y)), size // 2)
            pygame.draw.circle(surface, (0, 0, 0), (int(self.x), int(self.y)), size // 2, 2)
        elif self.stats.is_air:
            # Draw aircraft as triangles
            points = [
                (self.x + math.cos(self.angle) * size//2, self.y + math.sin(self.angle) * size//2),
                (self.x + math.cos(self.angle + 2.5) * size//3, self.y + math.sin(self.angle + 2.5) * size//3),
                (self.x + math.cos(self.angle - 2.5) * size//3, self.y + math.sin(self.angle - 2.5) * size//3),
            ]
            pygame.draw.polygon(surface, color, points)
            pygame.draw.polygon(surface, (0, 0, 0), points, 2)
        else:
            # Draw vehicles as rectangles with turret
            rect = pygame.Rect(self.x - size//2, self.y - size//2, size, size)
            pygame.draw.rect(surface, color, rect)
            pygame.draw.rect(surface, (0, 0, 0), rect, 2)
            
            # Draw turret direction
            turret_end_x = self.x + math.cos(self.angle) * size // 2
            turret_end_y = self.y + math.sin(self.angle) * size // 2
            pygame.draw.line(surface, (50, 50, 50), (self.x, self.y), (turret_end_x, turret_end_y), 4)
        
        # Draw harvester spice indicator
        if self.stats.can_harvest and self.spice_carried > 0:
            fill = self.spice_carried / HARVESTER_CAPACITY
            pygame.draw.rect(surface, COLORS['spice'], 
                           (self.x - size//2, self.y + size//2 + 2, size * fill, 4))
        
        # Draw selection indicator
        if self.selected:
            pygame.draw.circle(surface, COLORS['selection'], (int(self.x), int(self.y)), 
                             size//2 + 4, 2)
        
        # Draw health bar
        if self.health < self.max_health:
            self._draw_health_bar(surface, size)
    
    def _draw_health_bar(self, surface: pygame.Surface, size: int):
        """Draw health bar above unit"""
        health_pct = self.health / self.max_health
        bar_width = size
        bar_height = 4
        x = self.x - bar_width // 2
        y = self.y - size // 2 - 8
        
        # Background
        pygame.draw.rect(surface, (50, 50, 50), (x, y, bar_width, bar_height))
        
        # Health
        if health_pct > 0.6:
            color = COLORS['health_green']
        elif health_pct > 0.3:
            color = COLORS['health_yellow']
        else:
            color = COLORS['health_red']
        pygame.draw.rect(surface, color, (x, y, bar_width * health_pct, bar_height))


class Building:
    """Represents a game building"""
    def __init__(self, building_type: BuildingType, faction: Faction, tile_x: int, tile_y: int):
        self.building_type = building_type
        self.faction = faction
        self.stats = BUILDING_STATS[building_type]
        self.tile_x = tile_x
        self.tile_y = tile_y
        self.x = tile_x * TILE_SIZE
        self.y = tile_y * TILE_SIZE
        self.health = self.stats.health
        self.max_health = self.stats.health
        self.selected = False
        self.build_progress = 100  # Starts complete
        self.rally_point: Optional[Tuple[int, int]] = None
        self.attack_cooldown = 0
        
    @property
    def rect(self) -> pygame.Rect:
        return pygame.Rect(self.x, self.y, 
                          self.stats.size[0] * TILE_SIZE, 
                          self.stats.size[1] * TILE_SIZE)
    
    def get_color(self) -> Tuple[int, int, int]:
        if self.faction == Faction.ATREIDES:
            return COLORS['atreides']
        elif self.faction == Faction.HARKONNEN:
            return COLORS['harkonnen']
        else:
            return COLORS['ordos']
    
    def is_turret(self) -> bool:
        return self.building_type in (BuildingType.TURRET, BuildingType.ROCKET_TURRET)
    
    def update(self, dt: float, game: 'Game') -> Optional[Projectile]:
        """Update building, return projectile if turret attacks"""
        if self.attack_cooldown > 0:
            self.attack_cooldown -= dt
        
        # Turret attack logic
        if self.is_turret() and self.attack_cooldown <= 0:
            # Find enemy in range
            attack_range = 5 * TILE_SIZE if self.building_type == BuildingType.TURRET else 7 * TILE_SIZE
            center_x = self.x + self.stats.size[0] * TILE_SIZE / 2
            center_y = self.y + self.stats.size[1] * TILE_SIZE / 2
            
            for unit in game.units:
                if unit.faction != self.faction:
                    dist = math.sqrt((unit.x - center_x)**2 + (unit.y - center_y)**2)
                    if dist <= attack_range:
                        damage = 30 if self.building_type == BuildingType.TURRET else 50
                        projectile = Projectile(center_x, center_y, unit.x, unit.y, damage,
                                              color=(255, 100, 100))
                        self.attack_cooldown = 1.5
                        return projectile
        
        return None
    
    def take_damage(self, damage: int):
        """Apply damage to building"""
        self.health -= damage
        if self.health < 0:
            self.health = 0
    
    def draw(self, surface: pygame.Surface):
        """Draw the building"""
        color = self.get_color()
        rect = self.rect
        
        # Draw building base
        pygame.draw.rect(surface, COLORS['building_plate'], rect)
        
        # Draw building structure
        inner_rect = rect.inflate(-4, -4)
        pygame.draw.rect(surface, color, inner_rect)
        
        # Draw building-specific details
        self._draw_details(surface, rect, color)
        
        # Draw selection indicator
        if self.selected:
            pygame.draw.rect(surface, COLORS['selection'], rect, 3)
        
        # Draw health bar
        if self.health < self.max_health:
            self._draw_health_bar(surface)
    
    def _draw_details(self, surface: pygame.Surface, rect: pygame.Rect, color: Tuple[int, int, int]):
        """Draw building-specific visual details"""
        if self.building_type == BuildingType.CONSTRUCTION_YARD:
            # Draw crane
            pygame.draw.line(surface, (100, 100, 100), rect.center, 
                           (rect.centerx + 20, rect.top + 10), 3)
        elif self.building_type == BuildingType.WIND_TRAP:
            # Draw wind turbines
            for offset in [-15, 15]:
                pygame.draw.circle(surface, (200, 200, 200), 
                                 (rect.centerx + offset, rect.centery), 10, 2)
        elif self.building_type == BuildingType.REFINERY:
            # Draw silo tanks
            pygame.draw.rect(surface, COLORS['spice'], 
                           (rect.x + 10, rect.y + 10, 20, 30))
        elif self.building_type == BuildingType.TURRET:
            # Draw gun barrel
            pygame.draw.circle(surface, (100, 100, 100), rect.center, 12)
            pygame.draw.line(surface, (80, 80, 80), rect.center, 
                           (rect.centerx, rect.top + 5), 4)
        elif self.building_type == BuildingType.ROCKET_TURRET:
            # Draw rocket launcher
            pygame.draw.circle(surface, (100, 100, 100), rect.center, 12)
            for offset in [-5, 5]:
                pygame.draw.line(surface, (80, 80, 80), 
                               (rect.centerx + offset, rect.centery),
                               (rect.centerx + offset, rect.top + 5), 3)
        elif self.building_type == BuildingType.RADAR_OUTPOST:
            # Draw radar dish
            pygame.draw.arc(surface, (150, 150, 150), rect.inflate(-20, -20), 0, math.pi, 3)
        elif self.building_type == BuildingType.PALACE:
            # Draw palace spire
            pygame.draw.polygon(surface, (150, 150, 100), [
                (rect.centerx, rect.top + 5),
                (rect.centerx - 15, rect.centery),
                (rect.centerx + 15, rect.centery)
            ])
    
    def _draw_health_bar(self, surface: pygame.Surface):
        """Draw health bar above building"""
        health_pct = self.health / self.max_health
        bar_width = self.stats.size[0] * TILE_SIZE
        bar_height = 6
        x = self.x
        y = self.y - 10
        
        pygame.draw.rect(surface, (50, 50, 50), (x, y, bar_width, bar_height))
        
        if health_pct > 0.6:
            color = COLORS['health_green']
        elif health_pct > 0.3:
            color = COLORS['health_yellow']
        else:
            color = COLORS['health_red']
        pygame.draw.rect(surface, color, (x, y, bar_width * health_pct, bar_height))


class Player:
    """Represents a player/faction in the game"""
    def __init__(self, faction: Faction):
        self.faction = faction
        self.credits = STARTING_CREDITS
        self.power = 0
        self.power_usage = 0
        self.spice_capacity = 1000
        self.buildings_owned: List[BuildingType] = []
        
    def can_afford(self, cost: int) -> bool:
        return self.credits >= cost
    
    def spend(self, amount: int):
        self.credits -= amount
    
    def update_power(self, buildings: List[Building]):
        """Calculate total power production and usage"""
        self.power = 0
        self.power_usage = 0
        for building in buildings:
            if building.faction == self.faction:
                if building.stats.power > 0:
                    self.power += building.stats.power
                else:
                    self.power_usage += abs(building.stats.power)


class BuildQueue:
    """Manages building/unit production queue"""
    def __init__(self):
        self.queue: List[Tuple[str, any, float, float]] = []  # (type, item, progress, build_time)
        self.current_item = None
        
    def add(self, item_type: str, item, build_time: float):
        """Add item to build queue"""
        self.queue.append((item_type, item, 0.0, build_time))
    
    def update(self, dt: float, power_ok: bool) -> Optional[Tuple[str, any]]:
        """Update build progress, return completed item"""
        if not self.queue:
            return None
        
        item_type, item, progress, build_time = self.queue[0]
        
        # Slower building if low power
        speed_mult = 1.0 if power_ok else 0.3
        progress += (100 / build_time) * dt * speed_mult
        
        if progress >= 100:
            self.queue.pop(0)
            return (item_type, item)
        else:
            self.queue[0] = (item_type, item, progress, build_time)
            return None
    
    def get_progress(self) -> float:
        """Get current build progress"""
        if not self.queue:
            return 0
        return self.queue[0][2]


class Game:
    """Main game state manager"""
    def __init__(self):
        self.screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        pygame.display.set_caption("Dune II: The Building of a Dynasty")
        
        self.clock = pygame.time.Clock()
        # Initialize fonts - delay import to avoid circular import issues
        self.font = None
        self.font_large = None
        try:
            import pygame.font as pf
            pf.init()
            self.font = pf.Font(None, 20)
            self.font_large = pf.Font(None, 26)
        except Exception as e:
            print(f"Font init error: {e}")
        
        self.running = True
        self.paused = False
        
        # Initialize terrain
        self.terrain: List[List[TerrainType]] = []
        self._generate_terrain()
        
        # Game entities
        self.units: List[Unit] = []
        self.buildings: List[Building] = []
        self.projectiles: List[Projectile] = []
        
        # Players
        self.players: Dict[Faction, Player] = {
            Faction.ATREIDES: Player(Faction.ATREIDES),
            Faction.HARKONNEN: Player(Faction.HARKONNEN),
        }
        self.current_faction = Faction.ATREIDES
        
        # Selection
        self.selected_units: List[Unit] = []
        self.selected_building: Optional[Building] = None
        self.selection_start: Optional[Tuple[int, int]] = None
        self.selecting = False
        
        # Build queue
        self.build_queue = BuildQueue()
        self.placement_building: Optional[BuildingType] = None
        
        # UI state
        self.sidebar_scroll = 0
        self.show_minimap = True
        self.game_speed = 1.0
        
        # Initialize starting bases
        self._setup_starting_bases()
    
    def _generate_terrain(self):
        """Generate the game terrain with spice fields"""
        self.terrain = [[TerrainType.SAND for _ in range(MAP_TILES_X)] for _ in range(MAP_TILES_Y)]
        
        # Add rock formations
        for _ in range(15):
            cx = random.randint(2, MAP_TILES_X - 3)
            cy = random.randint(2, MAP_TILES_Y - 3)
            for _ in range(random.randint(5, 20)):
                nx = cx + random.randint(-3, 3)
                ny = cy + random.randint(-3, 3)
                if 0 <= nx < MAP_TILES_X and 0 <= ny < MAP_TILES_Y:
                    self.terrain[ny][nx] = TerrainType.ROCK
        
        # Add spice fields
        for _ in range(8):
            cx = random.randint(5, MAP_TILES_X - 6)
            cy = random.randint(5, MAP_TILES_Y - 6)
            for _ in range(random.randint(15, 40)):
                nx = cx + random.randint(-5, 5)
                ny = cy + random.randint(-5, 5)
                if 0 <= nx < MAP_TILES_X and 0 <= ny < MAP_TILES_Y:
                    if self.terrain[ny][nx] == TerrainType.SAND:
                        if random.random() < 0.3:
                            self.terrain[ny][nx] = TerrainType.SPICE_RICH
                        else:
                            self.terrain[ny][nx] = TerrainType.SPICE
    
    def _setup_starting_bases(self):
        """Set up starting bases for each faction"""
        # Atreides base (bottom-left)
        self._place_building(BuildingType.CONSTRUCTION_YARD, Faction.ATREIDES, 3, MAP_TILES_Y - 6)
        self._place_building(BuildingType.WIND_TRAP, Faction.ATREIDES, 1, MAP_TILES_Y - 4)
        self._place_building(BuildingType.REFINERY, Faction.ATREIDES, 7, MAP_TILES_Y - 5)
        self._place_building(BuildingType.BARRACKS, Faction.ATREIDES, 1, MAP_TILES_Y - 7)
        self._place_building(BuildingType.LIGHT_FACTORY, Faction.ATREIDES, 7, MAP_TILES_Y - 8)
        self._place_building(BuildingType.HEAVY_FACTORY, Faction.ATREIDES, 10, MAP_TILES_Y - 6)
        self._place_building(BuildingType.RADAR_OUTPOST, Faction.ATREIDES, 4, MAP_TILES_Y - 9)
        
        # Atreides starting units
        self._spawn_unit(UnitType.HARVESTER, Faction.ATREIDES, 9 * TILE_SIZE, (MAP_TILES_Y - 3) * TILE_SIZE)
        self._spawn_unit(UnitType.COMBAT_TANK, Faction.ATREIDES, 14 * TILE_SIZE, (MAP_TILES_Y - 4) * TILE_SIZE)
        self._spawn_unit(UnitType.COMBAT_TANK, Faction.ATREIDES, 15 * TILE_SIZE, (MAP_TILES_Y - 4) * TILE_SIZE)
        self._spawn_unit(UnitType.QUAD, Faction.ATREIDES, 14 * TILE_SIZE, (MAP_TILES_Y - 6) * TILE_SIZE)
        self._spawn_unit(UnitType.LIGHT_INFANTRY, Faction.ATREIDES, 4 * TILE_SIZE, (MAP_TILES_Y - 8) * TILE_SIZE)
        self._spawn_unit(UnitType.LIGHT_INFANTRY, Faction.ATREIDES, 5 * TILE_SIZE, (MAP_TILES_Y - 8) * TILE_SIZE)
        self._spawn_unit(UnitType.HEAVY_TROOPER, Faction.ATREIDES, 4 * TILE_SIZE, (MAP_TILES_Y - 9) * TILE_SIZE)
        
        # Harkonnen base (top-right)
        self._place_building(BuildingType.CONSTRUCTION_YARD, Faction.HARKONNEN, MAP_TILES_X - 6, 3)
        self._place_building(BuildingType.WIND_TRAP, Faction.HARKONNEN, MAP_TILES_X - 3, 3)
        self._place_building(BuildingType.REFINERY, Faction.HARKONNEN, MAP_TILES_X - 10, 3)
        self._place_building(BuildingType.BARRACKS, Faction.HARKONNEN, MAP_TILES_X - 3, 6)
        self._place_building(BuildingType.LIGHT_FACTORY, Faction.HARKONNEN, MAP_TILES_X - 10, 6)
        self._place_building(BuildingType.HEAVY_FACTORY, Faction.HARKONNEN, MAP_TILES_X - 6, 7)
        self._place_building(BuildingType.TURRET, Faction.HARKONNEN, MAP_TILES_X - 12, 5)
        self._place_building(BuildingType.TURRET, Faction.HARKONNEN, MAP_TILES_X - 12, 8)
        
        # Harkonnen starting units
        self._spawn_unit(UnitType.HARVESTER, Faction.HARKONNEN, (MAP_TILES_X - 8) * TILE_SIZE, 5 * TILE_SIZE)
        self._spawn_unit(UnitType.SIEGE_TANK, Faction.HARKONNEN, (MAP_TILES_X - 14) * TILE_SIZE, 6 * TILE_SIZE)
        self._spawn_unit(UnitType.COMBAT_TANK, Faction.HARKONNEN, (MAP_TILES_X - 15) * TILE_SIZE, 6 * TILE_SIZE)
        self._spawn_unit(UnitType.COMBAT_TANK, Faction.HARKONNEN, (MAP_TILES_X - 14) * TILE_SIZE, 7 * TILE_SIZE)
        self._spawn_unit(UnitType.QUAD, Faction.HARKONNEN, (MAP_TILES_X - 15) * TILE_SIZE, 8 * TILE_SIZE)
        self._spawn_unit(UnitType.TRIKE, Faction.HARKONNEN, (MAP_TILES_X - 16) * TILE_SIZE, 7 * TILE_SIZE)
        self._spawn_unit(UnitType.HEAVY_TROOPER, Faction.HARKONNEN, (MAP_TILES_X - 4) * TILE_SIZE, 8 * TILE_SIZE)
        self._spawn_unit(UnitType.HEAVY_TROOPER, Faction.HARKONNEN, (MAP_TILES_X - 5) * TILE_SIZE, 8 * TILE_SIZE)
        
        # Update power for both factions
        for faction in self.players:
            self.players[faction].update_power(self.buildings)
    
    def _place_building(self, building_type: BuildingType, faction: Faction, tile_x: int, tile_y: int):
        """Place a building on the map"""
        building = Building(building_type, faction, tile_x, tile_y)
        self.buildings.append(building)
        
        # Mark terrain as building plate
        for dx in range(building.stats.size[0]):
            for dy in range(building.stats.size[1]):
                if 0 <= tile_x + dx < MAP_TILES_X and 0 <= tile_y + dy < MAP_TILES_Y:
                    self.terrain[tile_y + dy][tile_x + dx] = TerrainType.BUILDING_PLATE
    
    def _spawn_unit(self, unit_type: UnitType, faction: Faction, x: float, y: float):
        """Spawn a unit at the given position"""
        unit = Unit(unit_type, faction, x, y)
        self.units.append(unit)
    
    def get_nearest_building(self, x: float, y: float, building_type: BuildingType, 
                            faction: Faction) -> Optional[Building]:
        """Find nearest building of given type for faction"""
        nearest = None
        nearest_dist = float('inf')
        
        for building in self.buildings:
            if building.building_type == building_type and building.faction == faction:
                dist = math.sqrt((building.x - x)**2 + (building.y - y)**2)
                if dist < nearest_dist:
                    nearest = building
                    nearest_dist = dist
        
        return nearest
    
    def find_nearest_spice(self, x: float, y: float) -> Optional[Tuple[int, int]]:
        """Find nearest spice tile"""
        nearest = None
        nearest_dist = float('inf')
        
        for ty in range(MAP_TILES_Y):
            for tx in range(MAP_TILES_X):
                if self.terrain[ty][tx] in (TerrainType.SPICE, TerrainType.SPICE_RICH):
                    dist = math.sqrt((tx * TILE_SIZE - x)**2 + (ty * TILE_SIZE - y)**2)
                    if dist < nearest_dist:
                        nearest = (tx, ty)
                        nearest_dist = dist
        
        return nearest
    
    def handle_events(self) -> bool:
        """Handle pygame events, return False if should quit"""
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    if self.placement_building:
                        self.placement_building = None
                    else:
                        return False
                elif event.key == pygame.K_SPACE:
                    self.paused = not self.paused
                elif event.key == pygame.K_1:
                    self._build_unit(UnitType.LIGHT_INFANTRY)
                elif event.key == pygame.K_2:
                    self._build_unit(UnitType.HEAVY_TROOPER)
                elif event.key == pygame.K_3:
                    self._build_unit(UnitType.TRIKE)
                elif event.key == pygame.K_4:
                    self._build_unit(UnitType.QUAD)
                elif event.key == pygame.K_5:
                    self._build_unit(UnitType.COMBAT_TANK)
                elif event.key == pygame.K_6:
                    self._build_unit(UnitType.HARVESTER)
                elif event.key == pygame.K_a:
                    # Attack move - select all and find enemy
                    if self.selected_units:
                        self._attack_move()
                elif event.key == pygame.K_s:
                    # Stop units
                    for unit in self.selected_units:
                        unit.target_x = None
                        unit.target_y = None
                        unit.attack_target = None
            
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1:  # Left click
                    self._handle_left_click(event.pos)
                elif event.button == 3:  # Right click
                    self._handle_right_click(event.pos)
            
            elif event.type == pygame.MOUSEBUTTONUP:
                if event.button == 1 and self.selecting:
                    self._handle_selection_end(event.pos)
                    self.selecting = False
            
            elif event.type == pygame.MOUSEMOTION:
                if self.selecting:
                    pass  # Selection box drawing handled in draw
        
        return True
    
    def _handle_left_click(self, pos: Tuple[int, int]):
        """Handle left mouse click"""
        x, y = pos
        
        # Check if clicking on sidebar
        if x >= MAP_WIDTH:
            self._handle_sidebar_click(x - MAP_WIDTH, y)
            return
        
        # Check if placing building
        if self.placement_building:
            tile_x = x // TILE_SIZE
            tile_y = y // TILE_SIZE
            if self._can_place_building(self.placement_building, tile_x, tile_y):
                stats = BUILDING_STATS[self.placement_building]
                if self.players[self.current_faction].can_afford(stats.cost):
                    self.players[self.current_faction].spend(stats.cost)
                    self._place_building(self.placement_building, self.current_faction, tile_x, tile_y)
                    self.players[self.current_faction].update_power(self.buildings)
            self.placement_building = None
            return
        
        # Start selection box
        self.selection_start = pos
        self.selecting = True
        
        # Check for unit selection (single click)
        clicked_unit = None
        for unit in self.units:
            if unit.rect.collidepoint(pos) and unit.faction == self.current_faction:
                clicked_unit = unit
                break
        
        # Check for building selection
        clicked_building = None
        for building in self.buildings:
            if building.rect.collidepoint(pos) and building.faction == self.current_faction:
                clicked_building = building
                break
        
        # Handle selection
        keys = pygame.key.get_pressed()
        if not keys[pygame.K_LSHIFT] and not keys[pygame.K_RSHIFT]:
            # Clear previous selection
            for unit in self.selected_units:
                unit.selected = False
            self.selected_units.clear()
            if self.selected_building:
                self.selected_building.selected = False
                self.selected_building = None
        
        if clicked_unit:
            clicked_unit.selected = True
            if clicked_unit not in self.selected_units:
                self.selected_units.append(clicked_unit)
        elif clicked_building:
            clicked_building.selected = True
            self.selected_building = clicked_building
    
    def _handle_right_click(self, pos: Tuple[int, int]):
        """Handle right mouse click (move/attack command)"""
        x, y = pos
        
        if x >= MAP_WIDTH:
            return
        
        # Check for attack target
        target_unit = None
        for unit in self.units:
            if unit.rect.collidepoint(pos) and unit.faction != self.current_faction:
                target_unit = unit
                break
        
        # Issue commands to selected units
        for unit in self.selected_units:
            if target_unit:
                unit.attack_target = target_unit
                unit.target_x = target_unit.x
                unit.target_y = target_unit.y
            else:
                unit.attack_target = None
                unit.target_x = x
                unit.target_y = y
    
    def _handle_selection_end(self, pos: Tuple[int, int]):
        """Handle end of selection box drag"""
        if not self.selection_start:
            return
        
        x1 = min(self.selection_start[0], pos[0])
        y1 = min(self.selection_start[1], pos[1])
        x2 = max(self.selection_start[0], pos[0])
        y2 = max(self.selection_start[1], pos[1])
        
        selection_rect = pygame.Rect(x1, y1, x2 - x1, y2 - y1)
        
        # Only do box selection if box is big enough
        if selection_rect.width > 5 and selection_rect.height > 5:
            keys = pygame.key.get_pressed()
            if not keys[pygame.K_LSHIFT] and not keys[pygame.K_RSHIFT]:
                for unit in self.selected_units:
                    unit.selected = False
                self.selected_units.clear()
            
            for unit in self.units:
                if unit.faction == self.current_faction and selection_rect.collidepoint(unit.x, unit.y):
                    unit.selected = True
                    if unit not in self.selected_units:
                        self.selected_units.append(unit)
        
        self.selection_start = None
    
    def _handle_sidebar_click(self, x: int, y: int):
        """Handle click on sidebar"""
        # Building buttons area
        if y >= 200 and y < 500:
            button_y = (y - 200) // 40
            building_types = [
                BuildingType.WIND_TRAP,
                BuildingType.REFINERY,
                BuildingType.SILO,
                BuildingType.BARRACKS,
                BuildingType.LIGHT_FACTORY,
                BuildingType.HEAVY_FACTORY,
                BuildingType.TURRET,
            ]
            if button_y < len(building_types):
                self.placement_building = building_types[button_y]
    
    def _can_place_building(self, building_type: BuildingType, tile_x: int, tile_y: int) -> bool:
        """Check if a building can be placed at the given position"""
        stats = BUILDING_STATS[building_type]
        
        # Check bounds
        if tile_x < 0 or tile_y < 0:
            return False
        if tile_x + stats.size[0] > MAP_TILES_X or tile_y + stats.size[1] > MAP_TILES_Y:
            return False
        
        # Check terrain
        for dx in range(stats.size[0]):
            for dy in range(stats.size[1]):
                terrain = self.terrain[tile_y + dy][tile_x + dx]
                if terrain not in (TerrainType.SAND, TerrainType.ROCK):
                    return False
        
        # Check for overlapping buildings
        new_rect = pygame.Rect(tile_x * TILE_SIZE, tile_y * TILE_SIZE,
                              stats.size[0] * TILE_SIZE, stats.size[1] * TILE_SIZE)
        for building in self.buildings:
            if building.rect.colliderect(new_rect):
                return False
        
        return True
    
    def _build_unit(self, unit_type: UnitType):
        """Start building a unit"""
        stats = UNIT_STATS[unit_type]
        player = self.players[self.current_faction]
        
        if player.can_afford(stats.cost):
            player.spend(stats.cost)
            self.build_queue.add("unit", unit_type, stats.build_time)
    
    def _attack_move(self):
        """Order selected units to attack-move towards enemies"""
        if not self.selected_units:
            return
        
        # Find nearest enemy
        for unit in self.selected_units:
            nearest_enemy = None
            nearest_dist = float('inf')
            
            for enemy in self.units:
                if enemy.faction != self.current_faction:
                    dist = math.sqrt((enemy.x - unit.x)**2 + (enemy.y - unit.y)**2)
                    if dist < nearest_dist:
                        nearest_enemy = enemy
                        nearest_dist = dist
            
            if nearest_enemy:
                unit.attack_target = nearest_enemy
                unit.target_x = nearest_enemy.x
                unit.target_y = nearest_enemy.y
    
    def update(self, dt: float):
        """Update game state"""
        if self.paused:
            return
        
        dt *= self.game_speed
        
        # Update build queue
        player = self.players[self.current_faction]
        power_ok = player.power >= player.power_usage
        completed = self.build_queue.update(dt, power_ok)
        
        if completed:
            item_type, item = completed
            if item_type == "unit":
                # Find a spawn location (near a relevant building)
                spawn_x, spawn_y = self._find_spawn_location(item)
                self._spawn_unit(item, self.current_faction, spawn_x, spawn_y)
        
        # Update units
        new_projectiles = []
        for unit in self.units:
            projectile = unit.update(dt, self)
            if projectile:
                new_projectiles.append(projectile)
        
        # Update buildings
        for building in self.buildings:
            projectile = building.update(dt, self)
            if projectile:
                new_projectiles.append(projectile)
        
        self.projectiles.extend(new_projectiles)
        
        # Update projectiles
        for projectile in self.projectiles[:]:
            if projectile.update():
                # Hit something - deal damage
                for unit in self.units:
                    if math.sqrt((unit.x - projectile.x)**2 + (unit.y - projectile.y)**2) < 20:
                        unit.take_damage(projectile.damage)
                        break
            
            if not projectile.active:
                self.projectiles.remove(projectile)
        
        # Remove dead units
        self.units = [u for u in self.units if u.health > 0]
        self.selected_units = [u for u in self.selected_units if u.health > 0]
        
        # Remove destroyed buildings
        self.buildings = [b for b in self.buildings if b.health > 0]
        
        # Simple AI for Harkonnen
        self._update_enemy_ai(dt)
    
    def _find_spawn_location(self, unit_type: UnitType) -> Tuple[float, float]:
        """Find a spawn location for a new unit"""
        stats = UNIT_STATS[unit_type]
        
        # Determine building to spawn from
        if stats.is_infantry:
            building_type = BuildingType.BARRACKS
        elif unit_type in (UnitType.TRIKE, UnitType.QUAD):
            building_type = BuildingType.LIGHT_FACTORY
        else:
            building_type = BuildingType.HEAVY_FACTORY
        
        # Find the building
        for building in self.buildings:
            if building.building_type == building_type and building.faction == self.current_faction:
                return (building.x + building.stats.size[0] * TILE_SIZE + TILE_SIZE,
                       building.y + building.stats.size[1] * TILE_SIZE // 2)
        
        # Default spawn location
        return (100, MAP_HEIGHT - 100)
    
    def _update_enemy_ai(self, dt: float):
        """Simple AI for enemy faction"""
        # Make Harkonnen units patrol and attack
        for unit in self.units:
            if unit.faction == Faction.HARKONNEN and unit.state == "idle":
                if not unit.stats.can_harvest:
                    # Find enemy to attack
                    for enemy in self.units:
                        if enemy.faction == Faction.ATREIDES:
                            dist = math.sqrt((enemy.x - unit.x)**2 + (enemy.y - unit.y)**2)
                            if dist < 400:
                                unit.attack_target = enemy
                                break
    
    def draw(self):
        """Draw the entire game"""
        # Draw terrain
        self._draw_terrain()
        
        # Draw buildings
        for building in self.buildings:
            building.draw(self.screen)
        
        # Draw units
        for unit in self.units:
            unit.draw(self.screen)
        
        # Draw projectiles
        for projectile in self.projectiles:
            projectile.draw(self.screen)
        
        # Draw selection box
        if self.selecting and self.selection_start:
            mouse_pos = pygame.mouse.get_pos()
            x1 = min(self.selection_start[0], mouse_pos[0])
            y1 = min(self.selection_start[1], mouse_pos[1])
            x2 = max(self.selection_start[0], mouse_pos[0])
            y2 = max(self.selection_start[1], mouse_pos[1])
            pygame.draw.rect(self.screen, COLORS['selection'], 
                           (x1, y1, x2 - x1, y2 - y1), 1)
        
        # Draw placement preview
        if self.placement_building:
            self._draw_placement_preview()
        
        # Draw sidebar
        self._draw_sidebar()
        
        pygame.display.flip()
    
    def _draw_terrain(self):
        """Draw the terrain tiles"""
        for y in range(MAP_TILES_Y):
            for x in range(MAP_TILES_X):
                terrain = self.terrain[y][x]
                rect = pygame.Rect(x * TILE_SIZE, y * TILE_SIZE, TILE_SIZE, TILE_SIZE)
                
                if terrain == TerrainType.SAND:
                    color = COLORS['sand'] if (x + y) % 2 == 0 else COLORS['sand_dark']
                elif terrain == TerrainType.ROCK:
                    color = COLORS['rock'] if (x + y) % 2 == 0 else COLORS['rock_dark']
                elif terrain == TerrainType.SPICE:
                    color = COLORS['spice']
                elif terrain == TerrainType.SPICE_RICH:
                    color = COLORS['spice_rich']
                else:
                    color = COLORS['building_plate']
                
                pygame.draw.rect(self.screen, color, rect)
    
    def _draw_placement_preview(self):
        """Draw building placement preview"""
        mouse_pos = pygame.mouse.get_pos()
        tile_x = mouse_pos[0] // TILE_SIZE
        tile_y = mouse_pos[1] // TILE_SIZE
        
        if mouse_pos[0] < MAP_WIDTH:
            stats = BUILDING_STATS[self.placement_building]
            rect = pygame.Rect(tile_x * TILE_SIZE, tile_y * TILE_SIZE,
                             stats.size[0] * TILE_SIZE, stats.size[1] * TILE_SIZE)
            
            can_place = self._can_place_building(self.placement_building, tile_x, tile_y)
            color = (0, 255, 0, 100) if can_place else (255, 0, 0, 100)
            
            # Draw semi-transparent preview
            s = pygame.Surface((rect.width, rect.height), pygame.SRCALPHA)
            s.fill((*color[:3], 100))
            self.screen.blit(s, rect.topleft)
            pygame.draw.rect(self.screen, color[:3], rect, 2)
    
    def _draw_text(self, text: str, x: int, y: int, color: Tuple[int, int, int], large: bool = False):
        """Helper to draw text safely, handling missing fonts"""
        font = self.font_large if large else self.font
        if font:
            surface = font.render(text, True, color)
            self.screen.blit(surface, (x, y))
    
    def _draw_sidebar(self):
        """Draw the game sidebar"""
        sidebar_rect = pygame.Rect(MAP_WIDTH, 0, SIDEBAR_WIDTH, SCREEN_HEIGHT)
        pygame.draw.rect(self.screen, COLORS['sidebar'], sidebar_rect)
        pygame.draw.line(self.screen, COLORS['sidebar_border'], 
                        (MAP_WIDTH, 0), (MAP_WIDTH, SCREEN_HEIGHT), 2)
        
        x = MAP_WIDTH + 10
        y = 10
        
        # Draw faction name
        faction_name = self.current_faction.name.title()
        self._draw_text(f"House {faction_name}", x, y, COLORS['atreides_light'], large=True)
        y += 30
        
        # Draw credits
        player = self.players[self.current_faction]
        self._draw_text(f"Credits: {player.credits}", x, y, COLORS['text_gold'])
        y += 20
        
        # Draw power
        power_color = COLORS['text'] if player.power >= player.power_usage else COLORS['health_red']
        self._draw_text(f"Power: {player.power}/{player.power_usage}", x, y, power_color)
        y += 30
        
        # Draw minimap
        self._draw_minimap(x, y, 180, 100)
        y += 110
        
        # Draw build queue progress
        if self.build_queue.queue:
            progress = self.build_queue.get_progress()
            pygame.draw.rect(self.screen, (50, 50, 50), (x, y, 180, 20))
            pygame.draw.rect(self.screen, COLORS['atreides'], (x, y, 180 * progress / 100, 20))
            self._draw_text(f"Building: {int(progress)}%", x + 5, y + 2, COLORS['text'])
        y += 30
        
        # Draw building buttons
        self._draw_text("Buildings", x, y, COLORS['text'], large=True)
        y += 25
        
        building_types = [
            BuildingType.WIND_TRAP,
            BuildingType.REFINERY,
            BuildingType.SILO,
            BuildingType.BARRACKS,
            BuildingType.LIGHT_FACTORY,
            BuildingType.HEAVY_FACTORY,
            BuildingType.TURRET,
        ]
        
        for bt in building_types:
            stats = BUILDING_STATS[bt]
            btn_rect = pygame.Rect(x, y, 180, 35)
            
            # Button background
            btn_color = COLORS['atreides'] if self.placement_building == bt else (80, 80, 100)
            pygame.draw.rect(self.screen, btn_color, btn_rect)
            pygame.draw.rect(self.screen, COLORS['sidebar_border'], btn_rect, 1)
            
            # Button text
            self._draw_text(f"{stats.name} (${stats.cost})", x + 5, y + 10, COLORS['text'])
            y += 40
        
        # Draw unit hotkeys
        y += 10
        self._draw_text("Units (1-6)", x, y, COLORS['text'], large=True)
        y += 25
        
        unit_hotkeys = [
            ("1: Infantry", UnitType.LIGHT_INFANTRY),
            ("2: Trooper", UnitType.HEAVY_TROOPER),
            ("3: Trike", UnitType.TRIKE),
            ("4: Quad", UnitType.QUAD),
            ("5: Tank", UnitType.COMBAT_TANK),
            ("6: Harvester", UnitType.HARVESTER),
        ]
        
        for label, ut in unit_hotkeys:
            stats = UNIT_STATS[ut]
            self._draw_text(f"{label} (${stats.cost})", x, y, COLORS['text'])
            y += 18
        
        # Draw selected unit info
        if self.selected_units:
            y = SCREEN_HEIGHT - 100
            self._draw_text(f"Selected: {len(self.selected_units)}", x, y, COLORS['text'], large=True)
            
            if len(self.selected_units) == 1:
                unit = self.selected_units[0]
                y += 20
                self._draw_text(f"{unit.stats.name}", x, y, COLORS['text'])
                y += 18
                self._draw_text(f"HP: {unit.health}/{unit.max_health}", x, y, COLORS['text'])
    
    def _draw_minimap(self, x: int, y: int, width: int, height: int):
        """Draw the minimap"""
        pygame.draw.rect(self.screen, COLORS['minimap_bg'], (x, y, width, height))
        
        scale_x = width / MAP_TILES_X
        scale_y = height / MAP_TILES_Y
        
        # Draw terrain
        for ty in range(MAP_TILES_Y):
            for tx in range(MAP_TILES_X):
                terrain = self.terrain[ty][tx]
                if terrain == TerrainType.SPICE or terrain == TerrainType.SPICE_RICH:
                    color = COLORS['spice']
                elif terrain == TerrainType.ROCK:
                    color = COLORS['rock']
                else:
                    continue
                
                px = x + int(tx * scale_x)
                py = y + int(ty * scale_y)
                pygame.draw.rect(self.screen, color, (px, py, max(1, int(scale_x)), max(1, int(scale_y))))
        
        # Draw buildings
        for building in self.buildings:
            color = building.get_color()
            px = x + int(building.tile_x * scale_x)
            py = y + int(building.tile_y * scale_y)
            w = max(2, int(building.stats.size[0] * scale_x))
            h = max(2, int(building.stats.size[1] * scale_y))
            pygame.draw.rect(self.screen, color, (px, py, w, h))
        
        # Draw units
        for unit in self.units:
            color = unit.get_color()
            px = x + int((unit.x / TILE_SIZE) * scale_x)
            py = y + int((unit.y / TILE_SIZE) * scale_y)
            pygame.draw.circle(self.screen, color, (px, py), 2)
        
        pygame.draw.rect(self.screen, COLORS['sidebar_border'], (x, y, width, height), 1)


# =============================================================================
# GLOBAL GAME STATE
# =============================================================================

game: Optional[Game] = None
clock = pygame.time.Clock()

def get_events():
    """Get pygame events - tracked by spacetimepy"""
    return pygame.event.get()

def save_screen(m, c, o, r):
    """Save screen as base64 image for monitoring"""
    buffer = io.BytesIO()
    pygame.image.save(pygame.display.get_surface(), buffer, "PNG")
    return {"image": base64.encodebytes(buffer.getvalue()).decode('utf-8')}


@spacetimepy.function(
    ignore=['clock'],
    return_hooks=[save_screen],
    track=[get_events, random.randint]
)
def display_game():
    """Main game loop function"""
    global game
    
    if game is None:
        game = Game()
    
    # Handle events
    if not game.handle_events():
        return False
    
    # Calculate delta time
    dt = clock.tick(FPS) / 1000.0
    
    # Update game state
    game.update(dt)
    
    # Draw everything
    game.draw()
    
    return True


if __name__ == "__main__":
    monitor = spacetimepy.init_monitoring(db_path="dune2.db", custom_picklers=["pygame"])
    spacetimepy.start_session("Dune 2")
    while display_game():
        pass
    spacetimepy.end_session()
    pygame.quit()
