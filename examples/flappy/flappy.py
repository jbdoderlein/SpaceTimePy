import base64
import io
import random

import pygame
import spacetimepy
from spacetimepy.picklers import pygame as pygame_pickler

# Initialize pygame
pygame.init()
random.seed(42)

# Screen dimensions
SCREEN_WIDTH = 500
SCREEN_HEIGHT = 700
SCREEN = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
pygame.display.set_caption("Flappy Bird")


# Game variables
BIRD_MOVEMENT = 0 
GAME_ACTIVE = True
FONT = pygame.font.SysFont("Arial", 30)

# Bird parameters
bird_rect = pygame.Rect(SCREEN_WIDTH // 3, SCREEN_HEIGHT // 2, 40, 40)

# Pipe list
pipes = []

# Clock object
clock = pygame.time.Clock()

def create_pipe():
    """Create a new pipe pair"""
    # Random position for the gap between top and bottom pipes
    gap_y_pos = random.randint(200, SCREEN_HEIGHT - 200)
    
    pipe_gap = 250
    # Bottom pipe starts at the gap position and extends to the bottom of the screen
    bottom_pipe = pygame.Rect(SCREEN_WIDTH, gap_y_pos + pipe_gap//2, 100, SCREEN_HEIGHT - gap_y_pos - pipe_gap//2)
    
    # Top pipe starts at the top of the screen and extends to the gap position
    top_pipe = pygame.Rect(SCREEN_WIDTH, 0, 100, gap_y_pos - pipe_gap//2)
    
    return bottom_pipe, top_pipe

def move_pipes():
    global pipes
    pipes_to_remove = []
    for pipe in pipes:
        pipe.x -= 3 # Pipe speed
        if pipe.right < 0: # Check if pipe is completely off-screen to the left
            pipes_to_remove.append(pipe)
        
    # Clean up passed_pipes list
    for p in pipes_to_remove:
        pipes.remove(p)

def draw_pipes():
    """Draw all pipes"""
    global pipes
    for pipe in pipes:
        if pipe.y == 0:  # Top pipe
            pygame.draw.rect(SCREEN, (0, 128, 0), pipe)
        else:  # Bottom pipe
            pygame.draw.rect(SCREEN, (0, 128, 0), pipe)

def check_collision(pipes, bird_rect):
    """Check if bird collides with pipes or goes off screen"""
    if bird_rect.top <= 0 or bird_rect.bottom >= SCREEN_HEIGHT:
        return True
    
    for pipe in pipes:
        if bird_rect.colliderect(pipe):
            return True
    
    return False

def reset_game():
    """Reset game state"""
    global BIRD_MOVEMENT, GAME_ACTIVE, SCORE, pipes
    bird_rect.y = SCREEN_HEIGHT // 2
    BIRD_MOVEMENT = 0
    pipes.clear()
    GAME_ACTIVE = True
    SCORE = 0

@spacetimepy.external
def get_events():
    return pygame.event.get()

def save_screen(_context: spacetimepy.CaptureReturnContext):
    buffer = io.BytesIO()
    pygame.image.save(pygame.display.get_surface(), buffer, "PNG")
    return {"image": base64.encodebytes(buffer.getvalue()).decode('utf-8')}

@spacetimepy.function(
        #ignore=['SCREEN','FONT', 'clock'], 
        return_hooks=[save_screen],
        )
def display_game():
    global GAME_ACTIVE, BIRD_MOVEMENT, pipes
    for event in get_events():
        if event.type == pygame.QUIT:
            return False
        
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_SPACE and GAME_ACTIVE:
                BIRD_MOVEMENT = -7
            
            if event.key == pygame.K_SPACE and not GAME_ACTIVE:
                reset_game()
    
    # Fill background
    SCREEN.fill((135, 206, 235))
    
    if GAME_ACTIVE:
        # Bird movement
        BIRD_MOVEMENT += 0.55 # Gravity
        bird_rect.y = int(bird_rect.y + BIRD_MOVEMENT)
        
        # Draw bird
        pygame.draw.rect(SCREEN, (255, 0, 0), bird_rect, border_radius=10)
        
        # Pipe logic
        if len(pipes) == 0 or pipes[-1].x < SCREEN_WIDTH - 300:
            bottom_pipe, top_pipe = create_pipe()
            pipes.append(bottom_pipe)
            pipes.append(top_pipe)
        
        move_pipes()
        draw_pipes()
        
        # Check collision
        if check_collision(pipes, bird_rect):
            GAME_ACTIVE = False

    else:
        # Game over screen
        game_over_text = FONT.render("Game Over!", True, (0, 0, 0))
        SCREEN.blit(game_over_text, (SCREEN_WIDTH//2 - 180, SCREEN_HEIGHT//2 - 15))
    
    
    # Update display
    pygame.display.update()
    clock.tick(60) 
    return True


if __name__ == "__main__":
    space = spacetimepy.SpaceTime.open(
        "flappy.db",
        custom_picklers=[pygame_pickler],
    )
    with space.capture.recording():
        while display_game():
            pass
    space.close()
    pygame.quit()
